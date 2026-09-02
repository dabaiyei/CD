from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.director import chapter_for_user
from app.api.routes.projects import project_for_user
from app.core.config import get_settings
from app.db.models import (
    AITask,
    AudioClip,
    AudioClipStatus,
    ChapterStatus,
    CompositionStatus,
    CompositionVersion,
    DialogueLine,
    DialogueVersion,
    Project,
    ProjectFile,
    ProjectFileKind,
    StoryboardShot,
    StoryboardVersion,
    User,
    VideoClip,
    VideoClipStatus,
    new_id,
)
from app.db.session import get_session
from app.domain.schemas import (
    CompositionPrepareRequest,
    CompositionVersionPublic,
    FinishingOptions,
    ProjectFilePublic,
    TaskPublic,
)
from app.services.composition import estimate_dialogue_duration
from app.services.media import save_project_audio
from app.services.object_storage import delete_media_file, persist_media_file
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import active_tasks, create_queued_task

router = APIRouter(prefix="/projects", tags=["chapter-finishing"])
MAX_AUDIO_UPLOAD_BYTES = 100 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "audio/webm",
    "audio/flac",
    "audio/mp4",
    "audio/aac",
}


async def composition_for_user(
    session: AsyncSession,
    *,
    project_id: str,
    chapter_id: str,
    composition_id: str,
    user: User,
) -> CompositionVersion:
    await chapter_for_user(session, project_id, chapter_id, user)
    composition = await session.get(CompositionVersion, composition_id)
    if (
        composition is None
        or composition.project_id != project_id
        or composition.chapter_id != chapter_id
        or composition.tenant_id != user.tenant_id
        or composition.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="成片版本不存在")
    return composition


@router.get("/{project_id}/finishing/options", response_model=FinishingOptions)
async def finishing_options(
    project_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FinishingOptions:
    await project_for_user(session, project_id, user)
    rows = list(
        (
            await session.scalars(
                select(ProjectFile)
                .where(
                    ProjectFile.project_id == project_id,
                    ProjectFile.tenant_id == user.tenant_id,
                    ProjectFile.user_id == user.id,
                    ProjectFile.kind == ProjectFileKind.AUDIO,
                )
                .order_by(ProjectFile.updated_at.desc())
            )
        ).all()
    )
    tracks = [row for row in rows if row.file_metadata.get("role") != "dialogue_audio"]
    return FinishingOptions(audio_files=[ProjectFilePublic.model_validate(row) for row in tracks])


@router.post(
    "/{project_id}/finishing/audio",
    response_model=ProjectFilePublic,
    status_code=status.HTTP_201_CREATED,
)
async def upload_finishing_audio(
    project_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectFile:
    await project_for_user(session, project_id, user)
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 MP3、WAV、M4A、AAC、OGG、WebM 或 FLAC 音频")
    data = await file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
    await file.close()
    if len(data) > MAX_AUDIO_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="音频文件不能超过 100 MB")
    file_id = new_id()
    _local_url, stored_path, mime_type = await run_in_threadpool(
        save_project_audio,
        data,
        uploads_root=get_settings().uploads_root,
        tenant_id=user.tenant_id,
        project_id=project_id,
        clip_id=file_id,
        content_type=content_type,
    )
    try:
        storage_key, media_url = await persist_media_file(stored_path, mime_type)
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    record = ProjectFile(
        id=file_id,
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        name=(file.filename or f"项目音轨{stored_path.suffix}")[:255],
        kind=ProjectFileKind.AUDIO,
        mime_type=mime_type,
        size_bytes=len(data),
        storage_path=storage_key,
        editable=False,
        file_metadata={"role": "soundtrack", "media_url": media_url, "uploaded_by": "user"},
    )
    session.add(record)
    try:
        await session.commit()
        await session.refresh(record)
    except Exception:
        await delete_media_file(storage_key, stored_path)
        await session.rollback()
        raise
    return record


@router.get(
    "/{project_id}/chapters/{chapter_id}/compositions",
    response_model=list[CompositionVersionPublic],
)
async def list_compositions(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[CompositionVersion]:
    await chapter_for_user(session, project_id, chapter_id, user)
    return list(
        (
            await session.scalars(
                select(CompositionVersion)
                .where(
                    CompositionVersion.chapter_id == chapter_id,
                    CompositionVersion.user_id == user.id,
                )
                .order_by(CompositionVersion.version.desc())
            )
        ).all()
    )


@router.get(
    "/{project_id}/chapters/{chapter_id}/compositions/{composition_id}",
    response_model=CompositionVersionPublic,
)
async def get_composition(
    project_id: str,
    chapter_id: str,
    composition_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CompositionVersion:
    return await composition_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        composition_id=composition_id,
        user=user,
    )


@router.post(
    "/{project_id}/chapters/{chapter_id}/compositions",
    response_model=CompositionVersionPublic,
    status_code=status.HTTP_201_CREATED,
)
async def prepare_composition(
    project_id: str,
    chapter_id: str,
    payload: CompositionPrepareRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CompositionVersion:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    project = await session.get(Project, project_id)
    storyboard = await session.scalar(
        select(StoryboardVersion).where(
            StoryboardVersion.chapter_id == chapter.id,
            StoryboardVersion.user_id == user.id,
            StoryboardVersion.is_active.is_(True),
        )
    )
    if project is None or storyboard is None:
        raise HTTPException(status_code=409, detail="成片前必须先选择生效分镜")
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
                select(VideoClip).where(
                    VideoClip.storyboard_version_id == storyboard.id,
                    VideoClip.user_id == user.id,
                    VideoClip.status == VideoClipStatus.READY,
                    VideoClip.is_active.is_(True),
                )
            )
        ).all()
    )
    clip_by_shot = {clip.shot_id: clip for clip in clips}
    missing_shots = [shot.order_index for shot in shots if shot.id not in clip_by_shot]
    if missing_shots:
        raise HTTPException(status_code=409, detail=f"镜头 {missing_shots[0]:02d} 尚无生效视频")

    dialogue = await session.scalar(
        select(DialogueVersion).where(
            DialogueVersion.chapter_id == chapter.id,
            DialogueVersion.user_id == user.id,
            DialogueVersion.is_active.is_(True),
        )
    )
    lines = (
        list(
            (
                await session.scalars(
                    select(DialogueLine)
                    .where(
                        DialogueLine.dialogue_version_id == dialogue.id,
                        DialogueLine.user_id == user.id,
                    )
                    .order_by(DialogueLine.order_index)
                )
            ).all()
        )
        if dialogue
        else []
    )
    audio_clips = (
        list(
            (
                await session.scalars(
                    select(AudioClip).where(
                        AudioClip.dialogue_version_id == dialogue.id,
                        AudioClip.user_id == user.id,
                        AudioClip.status == AudioClipStatus.READY,
                        AudioClip.is_active.is_(True),
                    )
                )
            ).all()
        )
        if dialogue
        else []
    )
    audio_by_line = {clip.dialogue_line_id: clip for clip in audio_clips}
    all_files = list(
        (
            await session.scalars(
                select(ProjectFile).where(
                    ProjectFile.project_id == project_id,
                    ProjectFile.tenant_id == user.tenant_id,
                    ProjectFile.user_id == user.id,
                )
            )
        ).all()
    )
    video_file_by_clip = {
        str(item.file_metadata.get("video_clip_id")): item
        for item in all_files
        if item.storage_path and item.file_metadata.get("video_clip_id")
    }
    audio_file_by_clip = {
        str(item.file_metadata.get("audio_clip_id")): item
        for item in all_files
        if item.storage_path and item.file_metadata.get("audio_clip_id")
    }
    selected_track_ids = [
        item
        for item in [payload.background_music_file_id, *payload.environment_audio_file_ids]
        if item
    ]
    track_by_id = {
        item.id: item
        for item in all_files
        if item.id in selected_track_ids and item.kind == ProjectFileKind.AUDIO and item.storage_path
    }
    if len(track_by_id) != len(set(selected_track_ids)):
        raise HTTPException(status_code=422, detail="选择的配乐或环境音不可用")

    line_by_shot: dict[str, list[DialogueLine]] = {}
    for line in lines:
        if line.shot_id:
            line_by_shot.setdefault(line.shot_id, []).append(line)
    timeline_shots: list[dict] = []
    cursor = Decimal("0")
    warnings: list[str] = []
    for shot in shots:
        video_clip = clip_by_shot[shot.id]
        video_file = video_file_by_clip.get(video_clip.id)
        if video_file is None:
            raise HTTPException(status_code=409, detail=f"镜头 {shot.order_index:02d} 视频文件已丢失")
        dialogue_cursor = Decimal("0.2")
        shot_dialogue: list[dict] = []
        for line in line_by_shot.get(shot.id, []):
            audio_clip = audio_by_line.get(line.id)
            audio_file = audio_file_by_clip.get(audio_clip.id) if audio_clip else None
            if audio_clip is None or audio_file is None:
                warnings.append(f"台词 {line.order_index:02d} 未加入音轨")
                continue
            duration = audio_clip.duration_seconds or estimate_dialogue_duration(line.text)
            shot_dialogue.append(
                {
                    "dialogue_line_id": line.id,
                    "audio_clip_id": audio_clip.id,
                    "speaker": line.speaker,
                    "duration_seconds": str(duration),
                    "timeline_start_seconds": str(cursor + dialogue_cursor),
                    "audio_storage_path": audio_file.storage_path,
                    "media_url": audio_clip.media_url,
                }
            )
            dialogue_cursor += duration + Decimal("0.12")
        if dialogue_cursor > shot.duration_seconds:
            warnings.append(f"镜头 {shot.order_index:02d} 台词时长超过镜头时长")
        timeline_shots.append(
            {
                "shot_id": shot.id,
                "order_index": shot.order_index,
                "title": shot.title,
                "duration_seconds": str(shot.duration_seconds),
                "timeline_start_seconds": str(cursor),
                "video_clip_id": video_clip.id,
                "video_storage_path": video_file.storage_path,
                "media_url": video_clip.media_url,
                "dialogue_clips": shot_dialogue,
            }
        )
        cursor += shot.duration_seconds

    background_music = (
        {
            "project_file_id": track_by_id[payload.background_music_file_id].id,
            "name": track_by_id[payload.background_music_file_id].name,
            "storage_path": track_by_id[payload.background_music_file_id].storage_path,
        }
        if payload.background_music_file_id
        else None
    )
    environment_audio = [
        {
            "project_file_id": track_by_id[file_id].id,
            "name": track_by_id[file_id].name,
            "storage_path": track_by_id[file_id].storage_path,
        }
        for file_id in payload.environment_audio_file_ids
    ]
    latest = await session.scalar(
        select(func.max(CompositionVersion.version)).where(CompositionVersion.chapter_id == chapter.id)
    )
    composition = CompositionVersion(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        chapter_id=chapter.id,
        storyboard_version_id=storyboard.id,
        dialogue_version_id=dialogue.id if dialogue else None,
        version=(latest or 0) + 1,
        title=payload.title or f"{chapter.title} 成片",
        duration_seconds=cursor,
        resolution=project.video_resolution,
        aspect_ratio=project.aspect_ratio,
        fps=payload.fps,
        timeline_manifest={
            "schema_version": 1,
            "duration_seconds": str(cursor),
            "shots": timeline_shots,
            "background_music": background_music,
            "environment_audio": environment_audio,
            "dialogue_volume": str(payload.dialogue_volume),
            "background_music_volume": str(payload.background_music_volume),
            "environment_volume": str(payload.environment_volume),
            "fade_seconds": str(payload.fade_seconds),
            "warnings": warnings,
        },
        status=CompositionStatus.DRAFT,
    )
    session.add(composition)
    await session.commit()
    await session.refresh(composition)
    return composition


@router.post(
    "/{project_id}/chapters/{chapter_id}/compositions/{composition_id}/render",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def render_composition(
    project_id: str,
    chapter_id: str,
    composition_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    composition = await composition_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        composition_id=composition_id,
        user=user,
    )
    if composition.status not in {CompositionStatus.DRAFT, CompositionStatus.FAILED}:
        raise HTTPException(status_code=409, detail="只有草稿或失败的成片版本可以渲染")
    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="chapter_composition_render",
    ):
        if pending.request_payload.get("composition_id") == composition.id:
            raise HTTPException(status_code=409, detail="该成片版本已有渲染任务")
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project_id,
        task_type="chapter_composition_render",
        model_id=None,
        cost=Decimal("0"),
        request_payload={
            "chapter_id": chapter_id,
            "composition_id": composition.id,
            "composition_version": composition.version,
        },
        message=f"{composition.title} 渲染",
    )
    composition.status = CompositionStatus.RENDERING
    composition.error_message = None
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task


@router.post(
    "/{project_id}/chapters/{chapter_id}/compositions/{composition_id}/activate",
    response_model=CompositionVersionPublic,
)
async def activate_composition(
    project_id: str,
    chapter_id: str,
    composition_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CompositionVersion:
    composition = await composition_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        composition_id=composition_id,
        user=user,
    )
    if composition.status != CompositionStatus.READY or not composition.output_url:
        raise HTTPException(status_code=409, detail="只能启用已完成渲染的成片版本")
    await session.execute(
        update(CompositionVersion)
        .where(CompositionVersion.chapter_id == chapter_id)
        .values(is_active=False)
    )
    composition.is_active = True
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    chapter.status = ChapterStatus.COMPLETED
    await session.commit()
    await session.refresh(composition)
    return composition
