from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.agent_chat import resolve_text_model
from app.api.routes.projects import project_for_user
from app.core.config import get_settings
from app.db.models import (
    AgentKind,
    AgentProfile,
    AITask,
    AssetExtraction,
    AudioClip,
    Chapter,
    ChapterAnalysis,
    ChapterStatus,
    DialogueVersion,
    ProjectFile,
    ProjectFileKind,
    ScriptReview,
    ScriptReviewDecision,
    ScriptVersion,
    SourceMode,
    StoryboardVersion,
    User,
)
from app.db.session import get_session
from app.domain.schemas import (
    ChapterAnalysisPublic,
    ChapterPublic,
    ProjectFileCreate,
    ProjectFileDetail,
    ProjectFilePublic,
    ProjectFileUpdate,
    ScriptGenerationRequest,
    ScriptReviewCreate,
    ScriptReviewPublic,
    ScriptReviewResult,
    ScriptVersionCreate,
    ScriptVersionPublic,
    SourceImportResult,
    TaskPublic,
)
from app.services.billing import resolve_task_pricing
from app.services.composition import invalidate_compositions
from app.services.object_storage import delete_media_file, materialize_media_file, object_storage
from app.services.source_import import (
    MAX_EPUB_BYTES,
    InvalidSourceFile,
    extract_chapters,
    parse_source,
    resolve_stored_file,
)
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import active_tasks, create_queued_task

router = APIRouter(prefix="/projects", tags=["director-workspace"])
settings = get_settings()


async def screenplay_agent(session: AsyncSession, tenant_id: str) -> AgentProfile:
    agent = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.kind == AgentKind.SCREENPLAY,
            AgentProfile.enabled.is_(True),
        )
    )
    if agent is None:
        raise HTTPException(status_code=409, detail="管理员尚未配置可用的剧本 Agent")
    return agent


async def project_file_for_user(
    session: AsyncSession,
    project_id: str,
    file_id: str,
    user: User,
) -> ProjectFile:
    await project_for_user(session, project_id, user)
    project_file = await session.get(ProjectFile, file_id)
    if (
        project_file is None
        or project_file.project_id != project_id
        or project_file.tenant_id != user.tenant_id
        or project_file.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="项目文件不存在")
    return project_file


async def chapter_for_user(
    session: AsyncSession,
    project_id: str,
    chapter_id: str,
    user: User,
) -> Chapter:
    await project_for_user(session, project_id, user)
    chapter = await session.get(Chapter, chapter_id)
    if (
        chapter is None
        or chapter.project_id != project_id
        or chapter.tenant_id != user.tenant_id
        or chapter.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="章节不存在")
    return chapter


async def activate_script(session: AsyncSession, chapter: Chapter, script: ScriptVersion) -> None:
    await session.execute(
        update(ScriptVersion).where(ScriptVersion.chapter_id == chapter.id).values(is_active=False)
    )
    script.is_active = True
    chapter.active_script_version_id = script.id
    chapter.status = ChapterStatus.REVIEWING
    reason = f"生效剧本已切换为 v{script.version}，需要重新生成"
    await invalidate_compositions(session, chapter_id=chapter.id, reason=reason)
    await session.execute(
        update(AssetExtraction)
        .where(
            AssetExtraction.chapter_id == chapter.id,
            AssetExtraction.script_version_id != script.id,
            AssetExtraction.is_active.is_(True),
        )
        .values(is_active=False, invalidated_reason=reason)
    )
    await session.execute(
        update(StoryboardVersion)
        .where(
            StoryboardVersion.chapter_id == chapter.id,
            StoryboardVersion.script_version_id != script.id,
            StoryboardVersion.is_active.is_(True),
        )
        .values(is_active=False, invalidated_reason=reason)
    )
    await session.execute(
        update(DialogueVersion)
        .where(
            DialogueVersion.chapter_id == chapter.id,
            DialogueVersion.script_version_id != script.id,
            DialogueVersion.is_active.is_(True),
        )
        .values(is_active=False, invalidated_reason=reason)
    )
    await session.execute(
        update(AudioClip)
        .where(AudioClip.chapter_id == chapter.id, AudioClip.is_active.is_(True))
        .values(is_active=False, invalidated_reason=reason)
    )


@router.get("/{project_id}/files", response_model=list[ProjectFilePublic])
async def list_project_files(
    project_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectFile]:
    await project_for_user(session, project_id, user)
    return list(
        (
            await session.scalars(
                select(ProjectFile)
                .where(
                    ProjectFile.project_id == project_id,
                    ProjectFile.tenant_id == user.tenant_id,
                    ProjectFile.user_id == user.id,
                )
                .order_by(ProjectFile.updated_at.desc())
            )
        ).all()
    )


@router.post(
    "/{project_id}/files",
    response_model=ProjectFileDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_file(
    project_id: str,
    payload: ProjectFileCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectFile:
    await project_for_user(session, project_id, user)
    project_file = ProjectFile(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        name=payload.name,
        kind=payload.kind,
        mime_type="text/markdown" if payload.name.lower().endswith(".md") else "text/plain",
        size_bytes=len(payload.content.encode("utf-8")),
        content=payload.content,
        editable=True,
        file_metadata={"created_by": "user"},
    )
    session.add(project_file)
    await session.commit()
    await session.refresh(project_file)
    return project_file


@router.get("/{project_id}/files/{file_id}", response_model=ProjectFileDetail)
async def get_project_file(
    project_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectFile:
    return await project_file_for_user(session, project_id, file_id, user)


@router.put("/{project_id}/files/{file_id}", response_model=ProjectFileDetail)
async def update_project_file(
    project_id: str,
    file_id: str,
    payload: ProjectFileUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectFile:
    project_file = await project_file_for_user(session, project_id, file_id, user)
    if not project_file.editable:
        raise HTTPException(status_code=409, detail="原始上传文件不可编辑")
    values = payload.model_dump(exclude_unset=True)
    for field, value in values.items():
        setattr(project_file, field, value)
    if payload.content is not None:
        project_file.size_bytes = len(payload.content.encode("utf-8"))
    await session.commit()
    await session.refresh(project_file)
    return project_file


@router.delete("/{project_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_file(
    project_id: str,
    file_id: str,
    delete_chapters: bool = False,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    project_file = await project_file_for_user(session, project_id, file_id, user)
    chapter_count = await session.scalar(
        select(func.count(Chapter.id)).where(Chapter.source_file_id == project_file.id)
    )
    if chapter_count and not delete_chapters:
        raise HTTPException(
            status_code=409,
            detail=f"该文件关联 {chapter_count} 个章节，确认删除章节及生产历史后才能继续",
        )
    storage_path = project_file.storage_path
    await session.delete(project_file)
    await session.commit()
    if storage_path:
        cached_path = (
            resolve_stored_file(settings.uploads_root, storage_path)
            if Path(storage_path).is_absolute()
            else settings.uploads_root / storage_path
        )
        await delete_media_file(storage_path, cached_path)


@router.get("/{project_id}/files/{file_id}/download")
async def download_project_file(
    project_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_file = await project_file_for_user(session, project_id, file_id, user)
    if not project_file.editable and project_file.storage_path:
        try:
            target = await materialize_media_file(project_file.storage_path)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=404, detail="项目文件内容已丢失") from error
        return FileResponse(target, media_type=project_file.mime_type, filename=project_file.name)
    filename = quote(project_file.name)
    return Response(
        content=(project_file.content or "").encode("utf-8"),
        media_type=f"{project_file.mime_type}; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/{project_id}/chapters", response_model=list[ChapterPublic])
async def list_chapters(
    project_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Chapter]:
    await project_for_user(session, project_id, user)
    return list(
        (
            await session.scalars(
                select(Chapter)
                .where(
                    Chapter.project_id == project_id,
                    Chapter.tenant_id == user.tenant_id,
                    Chapter.user_id == user.id,
                )
                .order_by(Chapter.created_at, Chapter.order_index)
            )
        ).all()
    )


@router.get(
    "/{project_id}/chapters/{chapter_id}/analyses",
    response_model=list[ChapterAnalysisPublic],
)
async def list_chapter_analyses(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ChapterAnalysis]:
    await chapter_for_user(session, project_id, chapter_id, user)
    return list(
        (
            await session.scalars(
                select(ChapterAnalysis)
                .where(
                    ChapterAnalysis.chapter_id == chapter_id,
                    ChapterAnalysis.user_id == user.id,
                )
                .order_by(ChapterAnalysis.version.desc())
            )
        ).all()
    )


@router.post(
    "/{project_id}/chapters/{chapter_id}/analyses/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_chapter_analysis(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="chapter_analysis_generation",
    ):
        if pending.request_payload.get("chapter_id") == chapter.id:
            raise HTTPException(status_code=409, detail="该章节已有分析任务正在处理")
    agent = await screenplay_agent(session, user.tenant_id)
    model, _provider, _api_key = await resolve_text_model(session, agent, user.tenant_id)
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="chapter_analysis_generation",
    )
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project_id,
        task_type="chapter_analysis_generation",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "chapter_id": chapter.id,
            "source_file_id": chapter.source_file_id,
            "source_hash": hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest(),
            "agent_profile_id": agent.id,
            "pricing": pricing.as_payload(),
        },
        message="章节分析",
    )
    chapter.status = ChapterStatus.ANALYZING
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task


@router.get(
    "/{project_id}/chapters/{chapter_id}/scripts",
    response_model=list[ScriptVersionPublic],
)
async def list_script_versions(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ScriptVersion]:
    await chapter_for_user(session, project_id, chapter_id, user)
    return list(
        (
            await session.scalars(
                select(ScriptVersion)
                .where(
                    ScriptVersion.chapter_id == chapter_id,
                    ScriptVersion.user_id == user.id,
                )
                .order_by(ScriptVersion.version.desc())
            )
        ).all()
    )


@router.post(
    "/{project_id}/chapters/{chapter_id}/scripts/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_script_version(
    project_id: str,
    chapter_id: str,
    payload: ScriptGenerationRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    analysis = (
        await session.get(ChapterAnalysis, payload.analysis_id)
        if payload.analysis_id
        else await session.scalar(
            select(ChapterAnalysis)
            .where(
                ChapterAnalysis.chapter_id == chapter.id,
                ChapterAnalysis.user_id == user.id,
            )
            .order_by(ChapterAnalysis.version.desc())
            .limit(1)
        )
    )
    if analysis is not None and (
        analysis.chapter_id != chapter.id
        or analysis.tenant_id != user.tenant_id
        or analysis.user_id != user.id
    ):
        raise HTTPException(status_code=422, detail="章节分析版本不可用")
    base_script = (
        await session.get(ScriptVersion, payload.base_script_version_id)
        if payload.base_script_version_id
        else None
    )
    if base_script is not None and (
        base_script.chapter_id != chapter.id
        or base_script.tenant_id != user.tenant_id
        or base_script.user_id != user.id
    ):
        raise HTTPException(status_code=422, detail="参考剧本版本不可用")
    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="chapter_script_generation",
    ):
        if pending.request_payload.get("chapter_id") == chapter.id:
            raise HTTPException(status_code=409, detail="该章节已有剧本生成任务正在处理")
    agent = await screenplay_agent(session, user.tenant_id)
    model, _provider, _api_key = await resolve_text_model(session, agent, user.tenant_id)
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="chapter_script_generation",
    )
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project_id,
        task_type="chapter_script_generation",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "chapter_id": chapter.id,
            "source_file_id": chapter.source_file_id,
            "source_hash": hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest(),
            "analysis_id": analysis.id if analysis else None,
            "analysis_version": analysis.version if analysis else None,
            "base_script_version_id": base_script.id if base_script else None,
            "base_script_version": base_script.version if base_script else None,
            "agent_profile_id": agent.id,
            "pricing": pricing.as_payload(),
        },
        message="AI 剧本生成",
    )
    chapter.status = ChapterStatus.SCRIPTING
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task


@router.post(
    "/{project_id}/chapters/{chapter_id}/scripts",
    response_model=ScriptVersionPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_script_version(
    project_id: str,
    chapter_id: str,
    payload: ScriptVersionCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ScriptVersion:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    latest = await session.scalar(
        select(func.max(ScriptVersion.version)).where(ScriptVersion.chapter_id == chapter.id)
    )
    script = ScriptVersion(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        chapter_id=chapter.id,
        version=(latest or 0) + 1,
        title=payload.title,
        content=payload.content,
        status=payload.status,
        review_notes=payload.review_notes,
        is_active=False,
    )
    session.add(script)
    await session.flush()
    if payload.activate:
        await activate_script(session, chapter, script)
    else:
        chapter.status = ChapterStatus.SCRIPTING
    await session.commit()
    await session.refresh(script)
    return script


@router.post(
    "/{project_id}/chapters/{chapter_id}/scripts/{script_id}/activate",
    response_model=ScriptVersionPublic,
)
async def activate_script_version(
    project_id: str,
    chapter_id: str,
    script_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ScriptVersion:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    script = await session.get(ScriptVersion, script_id)
    if (
        script is None
        or script.chapter_id != chapter.id
        or script.tenant_id != user.tenant_id
        or script.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="剧本版本不存在")
    await activate_script(session, chapter, script)
    await session.commit()
    await session.refresh(script)
    return script


@router.get(
    "/{project_id}/chapters/{chapter_id}/scripts/{script_id}/reviews",
    response_model=list[ScriptReviewPublic],
)
async def list_script_reviews(
    project_id: str,
    chapter_id: str,
    script_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ScriptReview]:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    script = await session.get(ScriptVersion, script_id)
    if (
        script is None
        or script.chapter_id != chapter.id
        or script.tenant_id != user.tenant_id
        or script.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="剧本版本不存在")
    return list(
        (
            await session.scalars(
                select(ScriptReview)
                .where(
                    ScriptReview.script_version_id == script.id,
                    ScriptReview.tenant_id == user.tenant_id,
                    ScriptReview.user_id == user.id,
                )
                .order_by(ScriptReview.created_at.desc(), ScriptReview.id.desc())
            )
        ).all()
    )


@router.post(
    "/{project_id}/chapters/{chapter_id}/scripts/{script_id}/reviews",
    response_model=ScriptReviewResult,
    status_code=status.HTTP_201_CREATED,
)
async def review_script_version(
    project_id: str,
    chapter_id: str,
    script_id: str,
    payload: ScriptReviewCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ScriptReviewResult:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    script = await session.get(ScriptVersion, script_id)
    if (
        script is None
        or script.chapter_id != chapter.id
        or script.tenant_id != user.tenant_id
        or script.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="剧本版本不存在")
    notes = payload.notes.strip()
    if payload.decision == ScriptReviewDecision.CHANGES_REQUESTED:
        if not notes:
            raise HTTPException(status_code=422, detail="退回修改时必须填写审核意见")
        if script.is_active or chapter.active_script_version_id == script.id:
            raise HTTPException(status_code=409, detail="生效中的剧本不能退回修改，请先切换生效版本")
        script.status = "draft"
        script.review_notes = notes
        if chapter.active_script_version_id is None:
            chapter.status = ChapterStatus.SCRIPTING
        activated = False
    else:
        script.status = "approved"
        script.review_notes = notes
        activated = payload.activate and not script.is_active
        if activated:
            await activate_script(session, chapter, script)
        elif chapter.active_script_version_id is None:
            chapter.status = ChapterStatus.REVIEWING
    review = ScriptReview(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        chapter_id=chapter.id,
        script_version_id=script.id,
        decision=payload.decision,
        notes=notes,
        reviewer_name=user.display_name,
        activated=activated,
    )
    session.add(review)
    await session.commit()
    await session.refresh(review)
    await session.refresh(script)
    return ScriptReviewResult(
        review=ScriptReviewPublic.model_validate(review),
        script=ScriptVersionPublic.model_validate(script),
    )


@router.delete(
    "/{project_id}/chapters/{chapter_id}/scripts/{script_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_script_version(
    project_id: str,
    chapter_id: str,
    script_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    script = await session.get(ScriptVersion, script_id)
    if (
        script is None
        or script.chapter_id != chapter.id
        or script.tenant_id != user.tenant_id
        or script.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="剧本版本不存在")
    if script.is_active or chapter.active_script_version_id == script.id:
        raise HTTPException(status_code=409, detail="生效中的剧本版本不能删除")
    await session.delete(script)
    await session.commit()


@router.post(
    "/{project_id}/sources/import",
    response_model=SourceImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_source(
    project_id: str,
    mode: SourceMode = Form(...),
    source_name: str = Form(default="粘贴文本"),
    pasted_text: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SourceImportResult:
    await project_for_user(session, project_id, user)
    if file is None and not (pasted_text or "").strip():
        raise HTTPException(status_code=422, detail="请上传 TXT/EPUB 文件或粘贴文本")

    original: ProjectFile | None = None
    stored_keys: list[str] = []
    if file is not None:
        filename = Path(file.filename or "source.txt").name
        data = await file.read(MAX_EPUB_BYTES + 1)
        await file.close()
        try:
            text = await run_in_threadpool(parse_source, data, filename)
        except InvalidSourceFile as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        suffix = Path(filename).suffix.lower()
        stored_relative = (
            Path(user.tenant_id) / "projects" / project_id / "files" / f"{uuid4().hex}{suffix}"
        ).as_posix()
        await object_storage().put_bytes(
            stored_relative,
            data,
            file.content_type or ("application/epub+zip" if suffix == ".epub" else "text/plain"),
        )
        stored_keys.append(stored_relative)
        if suffix == ".epub":
            original = ProjectFile(
                tenant_id=user.tenant_id,
                user_id=user.id,
                project_id=project_id,
                name=filename,
                kind=ProjectFileKind.SOURCE,
                mime_type="application/epub+zip",
                size_bytes=len(data),
                storage_path=stored_relative,
                editable=False,
                file_metadata={"role": "original_upload", "source_mode": mode.value},
            )
            session.add(original)
            source_filename = f"{Path(filename).stem}-提取文本.txt"
            source_storage = None
        else:
            source_filename = filename
            source_storage = stored_relative
    else:
        text = (pasted_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) > 2_000_000:
            raise HTTPException(status_code=413, detail="粘贴文本不能超过 200 万字符")
        source_filename = source_name.strip() or "粘贴文本"
        if not source_filename.lower().endswith(".txt"):
            source_filename += ".txt"
        source_storage = None

    source_file = ProjectFile(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        name=source_filename,
        kind=ProjectFileKind.SOURCE,
        mime_type="text/plain",
        size_bytes=len(text.encode("utf-8")),
        content=text,
        storage_path=source_storage,
        editable=True,
        file_metadata={"role": "chapter_source", "source_mode": mode.value},
    )
    session.add(source_file)
    await session.flush()
    parsed = await run_in_threadpool(extract_chapters, text, Path(source_filename).stem)
    chapters = [
        Chapter(
            tenant_id=user.tenant_id,
            user_id=user.id,
            project_id=project_id,
            source_file_id=source_file.id,
            source_mode=mode,
            order_index=index,
            title=item.title,
            original_content=item.content,
        )
        for index, item in enumerate(parsed, start=1)
    ]
    session.add_all(chapters)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        for stored_key in stored_keys:
            await object_storage().delete(stored_key)
        raise
    await session.refresh(source_file)
    if original:
        await session.refresh(original)
    for chapter in chapters:
        await session.refresh(chapter)
    return SourceImportResult(source_file=source_file, original_file=original, chapters=chapters)
