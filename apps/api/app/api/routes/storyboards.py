from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.director import chapter_for_user
from app.api.routes.projects import project_for_user
from app.db.models import (
    AgentKind,
    AgentProfile,
    AIModel,
    AITask,
    Asset,
    AssetExtraction,
    AssetExtractionItem,
    AssetStatus,
    AudioClip,
    Chapter,
    ChapterStatus,
    DialogueVersion,
    ModelType,
    Project,
    Provider,
    ScriptVersion,
    StoryboardShot,
    StoryboardVersion,
    User,
    VideoClip,
    VideoClipStatus,
)
from app.db.session import get_session
from app.domain.schemas import (
    StoryboardShotBatchRequest,
    StoryboardShotCreate,
    StoryboardShotPublic,
    StoryboardShotUpdate,
    StoryboardVersionCreate,
    StoryboardVersionDetail,
    StoryboardVersionPublic,
    TaskPublic,
    VideoClipPublic,
)
from app.services.billing import resolve_task_pricing
from app.services.composition import invalidate_compositions
from app.services.director_orchestration import (
    ensure_chapter_not_automating,
    start_storyboard_workflow_for_chapter,
)
from app.services.object_storage import materialize_media_file, object_key_from_media_url
from app.services.provider_adapters import closest_supported_video_duration, compatible_video_resolution
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import active_tasks, create_queued_task

router = APIRouter(prefix="/projects", tags=["storyboard-production"])


def safe_archive_name(value: str, *, fallback: str) -> str:
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .-")
    normalized = re.sub(r"\s+", " ", normalized)
    return (normalized or fallback)[:80]


def write_video_archive(target: Path, entries: list[tuple[Path, str]]) -> None:
    with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for source, archive_name in entries:
            archive.write(source, arcname=archive_name)


async def require_chapter_writable(session: AsyncSession, chapter: Chapter, user: User) -> None:
    try:
        await ensure_chapter_not_automating(session, chapter_id=chapter.id, user_id=user.id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def active_script_and_extraction(
    session: AsyncSession,
    chapter: Chapter,
) -> tuple[ScriptVersion, AssetExtraction]:
    if not chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="生成分镜前必须先选择生效剧本")
    script = await session.get(ScriptVersion, chapter.active_script_version_id)
    if script is None or script.chapter_id != chapter.id or script.user_id != chapter.user_id:
        raise HTTPException(status_code=409, detail="章节生效剧本不可用")
    extraction = await session.scalar(
        select(AssetExtraction).where(
            AssetExtraction.chapter_id == chapter.id,
            AssetExtraction.script_version_id == script.id,
            AssetExtraction.user_id == chapter.user_id,
            AssetExtraction.is_active.is_(True),
        )
    )
    if extraction is None:
        raise HTTPException(status_code=409, detail="生成分镜前必须先完成当前剧本的资产提取")
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .join(AssetExtractionItem, AssetExtractionItem.asset_id == Asset.id)
                .where(
                    AssetExtractionItem.extraction_id == extraction.id,
                    Asset.user_id == chapter.user_id,
                )
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )
    missing: list[str] = []
    for asset in assets:
        if asset.asset_type.value == "audio":
            continue
        key = object_key_from_media_url(asset.media_url)
        if asset.status != AssetStatus.READY or not key:
            missing.append(asset.name)
            continue
        try:
            await materialize_media_file(key)
        except (FileNotFoundError, ValueError):
            missing.append(asset.name)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"生成分镜前必须先完成资产图片：{'、'.join(missing[:8])}",
        )
    return script, extraction


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


async def validate_shot_assets(
    session: AsyncSession,
    *,
    asset_ids: list[str],
    project_id: str,
    tenant_id: str,
    user_id: str,
) -> None:
    if not asset_ids:
        return
    count = await session.scalar(
        select(func.count(Asset.id)).where(
            Asset.id.in_(asset_ids),
            Asset.project_id == project_id,
            Asset.tenant_id == tenant_id,
            Asset.user_id == user_id,
        )
    )
    if count != len(asset_ids):
        raise HTTPException(status_code=422, detail="镜头引用了不可用的项目资产")


async def project_video_model(
    session: AsyncSession,
    *,
    project_id: str,
    tenant_id: str,
) -> AIModel | None:
    project = await session.get(Project, project_id)
    model = await session.get(AIModel, project.video_model_id) if project and project.video_model_id else None
    if model is None:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.tenant_id == tenant_id,
                AIModel.model_type == ModelType.VIDEO,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if model is None or model.model_type != ModelType.VIDEO or not model.enabled:
        return None
    return model


async def normalize_shot_durations_for_project(
    session: AsyncSession,
    *,
    project_id: str,
    tenant_id: str,
    shots: list[StoryboardShotCreate],
) -> list[StoryboardShotCreate]:
    model = await project_video_model(session, project_id=project_id, tenant_id=tenant_id)
    return [
        shot.model_copy(
            update={
                "duration_seconds": Decimal(
                    str(
                        closest_supported_video_duration(
                            model.capabilities if model else None,
                            requested_duration=float(shot.duration_seconds),
                        )
                    )
                )
            }
        )
        for shot in shots
    ]


async def create_storyboard(
    session: AsyncSession,
    *,
    user: User,
    chapter: Chapter,
    script: ScriptVersion,
    shots: list[StoryboardShotCreate],
) -> tuple[StoryboardVersion, list[StoryboardShot]]:
    shots = await normalize_shot_durations_for_project(
        session,
        project_id=chapter.project_id,
        tenant_id=user.tenant_id,
        shots=shots,
    )
    await invalidate_compositions(
        session,
        chapter_id=chapter.id,
        reason="已生成新的分镜版本",
    )
    latest = await session.scalar(
        select(func.max(StoryboardVersion.version)).where(StoryboardVersion.chapter_id == chapter.id)
    )
    await session.execute(
        update(StoryboardVersion)
        .where(StoryboardVersion.chapter_id == chapter.id, StoryboardVersion.is_active.is_(True))
        .values(is_active=False, invalidated_reason="已生成新的分镜版本")
    )
    version = StoryboardVersion(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=chapter.project_id,
        chapter_id=chapter.id,
        script_version_id=script.id,
        version=(latest or 0) + 1,
        content=[
            {"order_index": index, **item.model_dump(mode="json")}
            for index, item in enumerate(shots, start=1)
        ],
        is_active=True,
    )
    session.add(version)
    await session.flush()
    records: list[StoryboardShot] = []
    for index, payload in enumerate(shots, start=1):
        await validate_shot_assets(
            session,
            asset_ids=payload.asset_ids,
            project_id=chapter.project_id,
            tenant_id=user.tenant_id,
            user_id=user.id,
        )
        shot = StoryboardShot(
            tenant_id=user.tenant_id,
            user_id=user.id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            storyboard_version_id=version.id,
            order_index=index,
            **payload.model_dump(),
        )
        session.add(shot)
        records.append(shot)
    chapter.status = ChapterStatus.STORYBOARD
    await session.flush()
    return version, records


async def storyboard_for_user(
    session: AsyncSession,
    *,
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    user: User,
) -> tuple[Chapter, StoryboardVersion]:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    storyboard = await session.get(StoryboardVersion, storyboard_id)
    if (
        storyboard is None
        or storyboard.chapter_id != chapter.id
        or storyboard.tenant_id != user.tenant_id
        or storyboard.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="分镜版本不存在")
    return chapter, storyboard


async def storyboard_shots_for_selection(
    session: AsyncSession,
    *,
    storyboard: StoryboardVersion,
    selection: StoryboardShotBatchRequest,
) -> list[StoryboardShot]:
    query = (
        select(StoryboardShot)
        .where(
            StoryboardShot.storyboard_version_id == storyboard.id,
            StoryboardShot.user_id == storyboard.user_id,
        )
        .order_by(StoryboardShot.order_index)
    )
    if selection.shot_ids:
        query = query.where(StoryboardShot.id.in_(selection.shot_ids))
    shots = list((await session.scalars(query)).all())
    if selection.shot_ids and len(shots) != len(selection.shot_ids):
        raise HTTPException(status_code=404, detail="部分镜头不存在")
    return shots


async def storyboard_text_agent_and_model(
    session: AsyncSession,
    *,
    user: User,
) -> tuple[AgentProfile, AIModel]:
    agent = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.tenant_id == user.tenant_id,
            AgentProfile.kind == AgentKind.SCREENPLAY,
            AgentProfile.enabled.is_(True),
        )
    )
    if agent is None or not agent.text_model_id:
        raise HTTPException(status_code=409, detail="管理员尚未配置可用的导演 Agent 与文本模型")
    model = await session.get(AIModel, agent.text_model_id)
    if model is None or model.model_type != ModelType.TEXT or not model.enabled:
        raise HTTPException(status_code=409, detail="导演 Agent 绑定的文本模型不可用")
    return agent, model


async def video_model_for_project(
    session: AsyncSession,
    *,
    project,
    user: User,
) -> AIModel:
    model = await session.get(AIModel, project.video_model_id) if project.video_model_id else None
    if model is None:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.tenant_id == user.tenant_id,
                AIModel.model_type == ModelType.VIDEO,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if (
        model is None
        or model.tenant_id != user.tenant_id
        or model.model_type != ModelType.VIDEO
        or not model.enabled
    ):
        raise HTTPException(status_code=409, detail="当前项目没有可用的视频模型")
    return model


def effective_video_resolution(model: AIModel, *, duration_seconds: float, project) -> str:
    if model.capabilities.get("schema_version") == 1:
        return compatible_video_resolution(
            model.capabilities,
            duration_seconds=duration_seconds,
            requested_resolution=str(project.video_resolution or "1080p"),
            aspect_ratio=str(project.aspect_ratio or "16:9"),
        )
    return str(project.video_resolution or "1080p")


async def queue_shot_video_task(
    session: AsyncSession,
    *,
    user: User,
    project,
    chapter: Chapter,
    storyboard: StoryboardVersion,
    shot: StoryboardShot,
    model: AIModel,
) -> tuple[AITask, object]:
    if not shot.video_prompt.strip():
        raise HTTPException(status_code=409, detail=f"镜头 {shot.order_index:02d} 缺少视频提示词")
    for pending in await active_tasks(
        session,
        project_id=project.id,
        task_type="shot_video_prompt_generation",
    ):
        if shot.id in (pending.request_payload.get("shot_ids") or []):
            raise HTTPException(
                status_code=409,
                detail=f"镜头 {shot.order_index:02d} 的视频提示词正在生成，请完成后再生成视频",
            )
    for pending in await active_tasks(
        session,
        project_id=project.id,
        task_type="shot_video_generation",
    ):
        if pending.request_payload.get("shot_id") == shot.id:
            raise HTTPException(status_code=409, detail=f"镜头 {shot.order_index:02d} 已有视频任务正在处理")
    latest = await session.scalar(select(func.max(VideoClip.version)).where(VideoClip.shot_id == shot.id))
    clip = VideoClip(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project.id,
        chapter_id=chapter.id,
        storyboard_version_id=storyboard.id,
        shot_id=shot.id,
        model_id=model.id,
        version=(latest or 0) + 1,
        status=VideoClipStatus.QUEUED,
    )
    session.add(clip)
    await session.flush()
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="shot_video_generation",
    )
    resolved_resolution = effective_video_resolution(
        model,
        duration_seconds=float(shot.duration_seconds),
        project=project,
    )
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project.id,
        task_type="shot_video_generation",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "chapter_id": chapter.id,
            "storyboard_version_id": storyboard.id,
            "shot_id": shot.id,
            "shot_version": shot.version,
            "video_clip_id": clip.id,
            "video_resolution": resolved_resolution,
            "requested_video_resolution": project.video_resolution,
            "aspect_ratio": project.aspect_ratio,
            "duration_seconds": float(shot.duration_seconds),
            "pricing": pricing.as_payload(),
        },
        message=f"镜头 {shot.order_index:02d} 视频生成",
    )
    chapter.status = ChapterStatus.VIDEO
    return task, event


@router.get(
    "/{project_id}/chapters/{chapter_id}/storyboards",
    response_model=list[StoryboardVersionPublic],
)
async def list_storyboards(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[StoryboardVersion]:
    await chapter_for_user(session, project_id, chapter_id, user)
    return list(
        (
            await session.scalars(
                select(StoryboardVersion)
                .where(
                    StoryboardVersion.chapter_id == chapter_id,
                    StoryboardVersion.user_id == user.id,
                )
                .order_by(StoryboardVersion.version.desc())
            )
        ).all()
    )


@router.get(
    "/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}",
    response_model=StoryboardVersionDetail,
)
async def get_storyboard(
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryboardVersionDetail:
    _chapter, storyboard = await storyboard_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=user,
    )
    shots = list(
        (
            await session.scalars(
                select(StoryboardShot)
                .where(
                    StoryboardShot.storyboard_version_id == storyboard.id,
                    StoryboardShot.user_id == user.id,
                )
                .order_by(StoryboardShot.order_index)
            )
        ).all()
    )
    clips = list(
        (
            await session.scalars(
                select(VideoClip)
                .where(
                    VideoClip.storyboard_version_id == storyboard.id,
                    VideoClip.user_id == user.id,
                )
                .order_by(VideoClip.shot_id, VideoClip.version.desc())
            )
        ).all()
    )
    return StoryboardVersionDetail(
        version=StoryboardVersionPublic.model_validate(storyboard),
        shots=[StoryboardShotPublic.model_validate(shot) for shot in shots],
        video_clips=[VideoClipPublic.model_validate(clip) for clip in clips],
    )


@router.get(
    "/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/videos/download",
    response_class=FileResponse,
)
async def download_storyboard_videos(
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    chapter, storyboard = await storyboard_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=user,
    )
    if not storyboard.is_active:
        raise HTTPException(status_code=409, detail="只能下载当前生效分镜的视频")
    rows = list(
        (
            await session.execute(
                select(StoryboardShot, VideoClip)
                .join(VideoClip, VideoClip.shot_id == StoryboardShot.id)
                .where(
                    StoryboardShot.storyboard_version_id == storyboard.id,
                    StoryboardShot.user_id == user.id,
                    VideoClip.storyboard_version_id == storyboard.id,
                    VideoClip.user_id == user.id,
                    VideoClip.status == VideoClipStatus.READY,
                    VideoClip.is_active.is_(True),
                    VideoClip.media_url.is_not(None),
                )
                .order_by(StoryboardShot.order_index)
            )
        ).all()
    )
    if not rows:
        raise HTTPException(status_code=409, detail="当前分镜还没有可下载的已完成视频")

    entries: list[tuple[Path, str]] = []
    for shot, clip in rows:
        storage_key = object_key_from_media_url(clip.media_url)
        if not storage_key:
            raise HTTPException(
                status_code=409,
                detail=f"镜头 {shot.order_index:02d} 的视频文件地址无效，请重新生成",
            )
        try:
            source = await materialize_media_file(storage_key)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(
                status_code=409,
                detail=f"镜头 {shot.order_index:02d} 的视频文件不存在，请重新生成",
            ) from error
        title = safe_archive_name(shot.title, fallback=f"镜头-{shot.order_index:03d}")
        entries.append((source, f"{shot.order_index:03d}-{title}-v{clip.version}.mp4"))

    temporary_dir = Path(tempfile.mkdtemp(prefix="cineforge-shot-videos-"))
    archive_path = temporary_dir / "videos.zip"
    try:
        await run_in_threadpool(write_video_archive, archive_path, entries)
    except Exception:
        await run_in_threadpool(shutil.rmtree, temporary_dir, True)
        raise
    filename = f"{safe_archive_name(chapter.title, fallback='chapter')}-镜头视频.zip"
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(shutil.rmtree, temporary_dir, True),
    )


@router.post(
    "/{project_id}/chapters/{chapter_id}/storyboards",
    response_model=StoryboardVersionDetail,
    status_code=status.HTTP_201_CREATED,
)
async def save_storyboard(
    project_id: str,
    chapter_id: str,
    payload: StoryboardVersionCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryboardVersionDetail:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    await require_chapter_writable(session, chapter, user)
    script, _extraction = await active_script_and_extraction(session, chapter)
    version, shots = await create_storyboard(
        session,
        user=user,
        chapter=chapter,
        script=script,
        shots=payload.shots,
    )
    await session.commit()
    await session.refresh(version)
    for shot in shots:
        await session.refresh(shot)
    return StoryboardVersionDetail(
        version=StoryboardVersionPublic.model_validate(version),
        shots=[StoryboardShotPublic.model_validate(shot) for shot in shots],
        video_clips=[],
    )


@router.post(
    "/{project_id}/chapters/{chapter_id}/storyboards/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_storyboard(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    await require_chapter_writable(session, chapter, user)
    try:
        workflow = await start_storyboard_workflow_for_chapter(
            session,
            chapter=chapter,
            user=user,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not workflow.current_task_id:
        raise HTTPException(status_code=409, detail="导演分镜工作流未能创建任务")
    task = await session.get(AITask, workflow.current_task_id)
    if task is None:
        raise HTTPException(status_code=409, detail="导演分镜工作流任务不存在")
    await session.refresh(task)
    return task


@router.post(
    "/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/activate",
    response_model=StoryboardVersionPublic,
)
async def activate_storyboard(
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryboardVersion:
    chapter, storyboard = await storyboard_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=user,
    )
    await require_chapter_writable(session, chapter, user)
    if storyboard.script_version_id != chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="只能启用当前生效剧本生成的分镜版本")
    reason = f"已切换为分镜 v{storyboard.version}"
    await invalidate_compositions(session, chapter_id=chapter.id, reason=reason)
    await session.execute(
        update(StoryboardVersion).where(StoryboardVersion.chapter_id == chapter.id).values(is_active=False)
    )
    await session.execute(
        update(VideoClip)
        .where(
            VideoClip.chapter_id == chapter.id,
            VideoClip.storyboard_version_id != storyboard.id,
            VideoClip.is_active.is_(True),
        )
        .values(is_active=False, invalidated_reason=reason)
    )
    outdated_dialogues = select(DialogueVersion.id).where(
        DialogueVersion.chapter_id == chapter.id,
        DialogueVersion.storyboard_version_id.is_not(None),
        DialogueVersion.storyboard_version_id != storyboard.id,
        DialogueVersion.is_active.is_(True),
    )
    await session.execute(
        update(AudioClip)
        .where(AudioClip.dialogue_version_id.in_(outdated_dialogues), AudioClip.is_active.is_(True))
        .values(is_active=False, invalidated_reason=reason)
    )
    await session.execute(
        update(DialogueVersion)
        .where(
            DialogueVersion.chapter_id == chapter.id,
            DialogueVersion.storyboard_version_id.is_not(None),
            DialogueVersion.storyboard_version_id != storyboard.id,
            DialogueVersion.is_active.is_(True),
        )
        .values(is_active=False, invalidated_reason=reason)
    )
    storyboard.is_active = True
    storyboard.invalidated_reason = None
    chapter.status = ChapterStatus.STORYBOARD
    await session.commit()
    await session.refresh(storyboard)
    return storyboard


@router.patch(
    "/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/shots/{shot_id}",
    response_model=StoryboardShotPublic,
)
async def update_storyboard_shot(
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    shot_id: str,
    payload: StoryboardShotUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryboardShot:
    chapter, storyboard = await storyboard_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=user,
    )
    await require_chapter_writable(session, chapter, user)
    if not storyboard.is_active:
        raise HTTPException(status_code=409, detail="只能编辑当前生效分镜")
    shot = await session.get(StoryboardShot, shot_id)
    if (
        shot is None
        or shot.storyboard_version_id != storyboard.id
        or shot.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="镜头不存在")
    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="shot_video_generation",
    ):
        if pending.request_payload.get("shot_id") == shot.id:
            raise HTTPException(status_code=409, detail="该镜头正在生成视频，完成后才能编辑")
    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="shot_video_prompt_generation",
    ):
        if shot.id in (pending.request_payload.get("shot_ids") or []):
            raise HTTPException(status_code=409, detail="该镜头正在生成视频提示词，完成后才能编辑")
    values = payload.model_dump(exclude_unset=True)
    if "asset_ids" in values:
        await validate_shot_assets(
            session,
            asset_ids=values["asset_ids"],
            project_id=project_id,
            tenant_id=user.tenant_id,
            user_id=user.id,
        )
    if "duration_seconds" in values:
        model = await project_video_model(session, project_id=project_id, tenant_id=user.tenant_id)
        values["duration_seconds"] = Decimal(
            str(
                closest_supported_video_duration(
                    model.capabilities if model else None,
                    requested_duration=float(values["duration_seconds"]),
                )
            )
        )
    for field, value in values.items():
        setattr(shot, field, value)
    shot.version += 1
    await invalidate_compositions(
        session,
        chapter_id=chapter.id,
        reason=f"镜头 {shot.order_index:02d} 已更新",
    )
    await session.execute(
        update(VideoClip)
        .where(VideoClip.shot_id == shot.id, VideoClip.is_active.is_(True))
        .values(is_active=False, invalidated_reason=f"镜头内容已更新为 v{shot.version}")
    )
    dialogue_reason = f"镜头内容已更新为 v{shot.version}，需要重新提取台词"
    affected_dialogues = select(DialogueVersion.id).where(
        DialogueVersion.storyboard_version_id == storyboard.id,
        DialogueVersion.is_active.is_(True),
    )
    await session.execute(
        update(AudioClip)
        .where(AudioClip.dialogue_version_id.in_(affected_dialogues), AudioClip.is_active.is_(True))
        .values(is_active=False, invalidated_reason=dialogue_reason)
    )
    await session.execute(
        update(DialogueVersion)
        .where(
            DialogueVersion.storyboard_version_id == storyboard.id,
            DialogueVersion.is_active.is_(True),
        )
        .values(is_active=False, invalidated_reason=dialogue_reason)
    )
    chapter.status = ChapterStatus.STORYBOARD
    await session.commit()
    await session.refresh(shot)
    return shot


@router.post(
    "/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-prompts/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_storyboard_video_prompts(
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    payload: StoryboardShotBatchRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    project = await project_for_user(session, project_id, user)
    chapter, storyboard = await storyboard_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=user,
    )
    await require_chapter_writable(session, chapter, user)
    if not storyboard.is_active or storyboard.script_version_id != chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="只能为当前生效分镜生成视频提示词")
    shots = await storyboard_shots_for_selection(session, storyboard=storyboard, selection=payload)
    if not payload.overwrite:
        shots = [shot for shot in shots if not shot.video_prompt.strip()]
    if not shots:
        raise HTTPException(status_code=409, detail="没有需要生成视频提示词的镜头")
    active_video_shot_ids = {
        str(pending.request_payload.get("shot_id"))
        for pending in await active_tasks(
            session,
            project_id=project_id,
            task_type="shot_video_generation",
        )
        if pending.request_payload.get("shot_id")
    }
    shots = [shot for shot in shots if shot.id not in active_video_shot_ids]
    if not shots:
        raise HTTPException(status_code=409, detail="所选镜头正在生成视频，暂时不能重写提示词")
    selected_ids = {shot.id for shot in shots}
    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="shot_video_prompt_generation",
    ):
        pending_ids = {
            str(item)
            for item in (pending.request_payload.get("shot_ids") or [])
            if isinstance(item, str)
        }
        if selected_ids & pending_ids:
            raise HTTPException(status_code=409, detail="所选镜头已有视频提示词任务正在处理")
    agent, model = await storyboard_text_agent_and_model(session, user=user)
    video_model = await video_model_for_project(session, project=project, user=user)
    video_provider = await session.get(Provider, video_model.provider_id)
    if video_provider is None or video_provider.tenant_id != user.tenant_id:
        raise HTTPException(status_code=409, detail="当前项目视频模型平台不可用")
    resolved_resolution = effective_video_resolution(
        video_model,
        duration_seconds=float(shots[0].duration_seconds),
        project=project,
    )
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="shot_video_prompt_generation",
        quantity=len(shots),
    )
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project.id,
        task_type="shot_video_prompt_generation",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "chapter_id": chapter.id,
            "storyboard_version_id": storyboard.id,
            "shot_ids": [shot.id for shot in shots],
            "shot_versions": {shot.id: shot.version for shot in shots},
            "overwrite": payload.overwrite,
            "agent_profile_id": agent.id,
            "video_resolution": resolved_resolution,
            "requested_video_resolution": project.video_resolution,
            "aspect_ratio": project.aspect_ratio,
            "target_video_model": {
                "provider_code": video_provider.code,
                "provider_name": video_provider.name,
                "model_id": video_model.model_id,
                "model_name": video_model.name,
                "capabilities": video_model.capabilities or {},
            },
            "pricing": pricing.as_payload(),
        },
        message=f"{len(shots)} 个镜头视频提示词生成",
    )
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task


@router.post(
    "/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/videos/generate",
    response_model=list[TaskPublic],
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_storyboard_videos(
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    payload: StoryboardShotBatchRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AITask]:
    project = await project_for_user(session, project_id, user)
    chapter, storyboard = await storyboard_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=user,
    )
    await require_chapter_writable(session, chapter, user)
    if not storyboard.is_active or storyboard.script_version_id != chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="只能为当前生效分镜生成视频")
    shots = await storyboard_shots_for_selection(session, storyboard=storyboard, selection=payload)
    if payload.only_missing:
        active_clip_shot_ids = set(
            (
                await session.scalars(
                    select(VideoClip.shot_id).where(
                        VideoClip.storyboard_version_id == storyboard.id,
                        VideoClip.is_active.is_(True),
                        VideoClip.status == VideoClipStatus.READY,
                    )
                )
            ).all()
        )
        shots = [shot for shot in shots if shot.id not in active_clip_shot_ids]
    if not shots:
        raise HTTPException(status_code=409, detail="没有需要生成视频的镜头")
    model = await video_model_for_project(session, project=project, user=user)
    queued = [
        await queue_shot_video_task(
            session,
            user=user,
            project=project,
            chapter=chapter,
            storyboard=storyboard,
            shot=shot,
            model=model,
        )
        for shot in shots
    ]
    tasks = [item[0] for item in queued]
    await session.commit()
    for task in tasks:
        await session.refresh(task)
    for task, event in queued:
        await enqueue_task(task.id)
        await publish_task_event(task, event)
    return tasks


@router.post(
    "/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/shots/{shot_id}/videos/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_shot_video(
    project_id: str,
    chapter_id: str,
    storyboard_id: str,
    shot_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    project = await project_for_user(session, project_id, user)
    chapter, storyboard = await storyboard_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        storyboard_id=storyboard_id,
        user=user,
    )
    await require_chapter_writable(session, chapter, user)
    if not storyboard.is_active or storyboard.script_version_id != chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="只能为当前生效分镜生成视频")
    shot = await session.get(StoryboardShot, shot_id)
    if (
        shot is None
        or shot.storyboard_version_id != storyboard.id
        or shot.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="镜头不存在")
    model = await video_model_for_project(session, project=project, user=user)
    task, event = await queue_shot_video_task(
        session,
        user=user,
        project=project,
        chapter=chapter,
        storyboard=storyboard,
        shot=shot,
        model=model,
    )
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task
