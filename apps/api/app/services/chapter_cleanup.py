from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentMemory,
    AITask,
    Asset,
    AssetExtraction,
    AssetExtractionItem,
    AssetRevision,
    AudioClip,
    Chapter,
    ChapterAnalysis,
    ChapterStatus,
    CompositionVersion,
    DialogueVersion,
    DirectorChildRun,
    DirectorWorkflowRun,
    ProjectFile,
    ScriptVersion,
    StoryboardShot,
    StoryboardVersion,
    TaskStatus,
    VideoClip,
)
from app.services.object_storage import object_key_from_media_url


class ChapterHasActiveTasksError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChapterCleanupResult:
    storage_paths: set[str]
    deleted_asset_ids: set[str]
    deleted_task_ids: set[str]


def _contains_resource(value: Any, resource_ids: set[str]) -> bool:
    if isinstance(value, str):
        return value in resource_ids
    if isinstance(value, dict):
        return any(_contains_resource(item, resource_ids) for item in value.values())
    if isinstance(value, list):
        return any(_contains_resource(item, resource_ids) for item in value)
    return False


def _media_storage_path(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return object_key_from_media_url(value)
    except ValueError:
        return None


def _collect_media_paths(values: Iterable[str | None]) -> set[str]:
    return {path for value in values if (path := _media_storage_path(value))}


def _reference_audio_url(metadata: dict[str, Any] | None) -> str | None:
    value = (metadata or {}).get("reference_audio_url")
    return str(value) if isinstance(value, str) and value else None


async def _chapter_assets(
    session: AsyncSession,
    *,
    chapter: Chapter,
    extraction_ids: set[str],
) -> list[Asset]:
    asset_ids = set(
        (
            await session.scalars(
                select(AssetExtractionItem.asset_id).where(
                    AssetExtractionItem.extraction_id.in_(extraction_ids)
                )
            )
        ).all()
    )
    if not asset_ids:
        return []

    project_assets = list(
        (
            await session.scalars(
                select(Asset).where(
                    Asset.project_id == chapter.project_id,
                    Asset.tenant_id == chapter.tenant_id,
                    Asset.user_id == chapter.user_id,
                )
            )
        ).all()
    )
    changed = True
    while changed:
        changed = False
        for asset in project_assets:
            if asset.parent_asset_id in asset_ids and asset.id not in asset_ids:
                asset_ids.add(asset.id)
                changed = True
    return [asset for asset in project_assets if asset.id in asset_ids]


async def _surviving_storage_paths(session: AsyncSession) -> set[str]:
    surviving: set[str] = set()
    media_columns = (
        Asset.media_url,
        AssetRevision.media_url,
        VideoClip.media_url,
        AudioClip.media_url,
        CompositionVersion.output_url,
        StoryboardShot.reference_image_url,
    )
    for column in media_columns:
        values = (await session.scalars(select(column).where(column.is_not(None)))).all()
        surviving.update(_collect_media_paths(values))
    asset_metadata = (await session.scalars(select(Asset.asset_metadata))).all()
    revision_metadata = (await session.scalars(select(AssetRevision.asset_metadata))).all()
    surviving.update(
        _collect_media_paths(
            _reference_audio_url(metadata)
            for metadata in [*asset_metadata, *revision_metadata]
        )
    )
    file_paths = (
        await session.scalars(
            select(ProjectFile.storage_path).where(ProjectFile.storage_path.is_not(None))
        )
    ).all()
    surviving.update(str(path) for path in file_paths if path)
    return surviving


async def purge_chapter_production(
    session: AsyncSession,
    *,
    chapter: Chapter,
    delete_chapter: bool,
) -> ChapterCleanupResult:
    extraction_ids = set(
        (
            await session.scalars(
                select(AssetExtraction.id).where(AssetExtraction.chapter_id == chapter.id)
            )
        ).all()
    )
    assets = await _chapter_assets(session, chapter=chapter, extraction_ids=extraction_ids)
    asset_ids = {asset.id for asset in assets}
    workflow_ids = set(
        (
            await session.scalars(
                select(DirectorWorkflowRun.id).where(DirectorWorkflowRun.chapter_id == chapter.id)
            )
        ).all()
    )
    child_task_ids = set(
        (
            await session.scalars(
                select(DirectorChildRun.task_id).where(
                    DirectorChildRun.workflow_id.in_(workflow_ids),
                    DirectorChildRun.task_id.is_not(None),
                )
            )
        ).all()
    )

    analyses = list(
        (await session.scalars(select(ChapterAnalysis).where(ChapterAnalysis.chapter_id == chapter.id))).all()
    )
    scripts = list(
        (await session.scalars(select(ScriptVersion).where(ScriptVersion.chapter_id == chapter.id))).all()
    )
    storyboards = list(
        (
            await session.scalars(
                select(StoryboardVersion).where(StoryboardVersion.chapter_id == chapter.id)
            )
        ).all()
    )
    shots = list(
        (await session.scalars(select(StoryboardShot).where(StoryboardShot.chapter_id == chapter.id))).all()
    )
    video_clips = list(
        (await session.scalars(select(VideoClip).where(VideoClip.chapter_id == chapter.id))).all()
    )
    dialogues = list(
        (await session.scalars(select(DialogueVersion).where(DialogueVersion.chapter_id == chapter.id))).all()
    )
    audio_clips = list(
        (await session.scalars(select(AudioClip).where(AudioClip.chapter_id == chapter.id))).all()
    )
    compositions = list(
        (
            await session.scalars(
                select(CompositionVersion).where(CompositionVersion.chapter_id == chapter.id)
            )
        ).all()
    )

    resource_ids = {
        chapter.id,
        *extraction_ids,
        *asset_ids,
        *workflow_ids,
        *child_task_ids,
        *(item.id for item in analyses),
        *(item.id for item in scripts),
        *(item.id for item in storyboards),
        *(item.id for item in shots),
        *(item.id for item in video_clips),
        *(item.id for item in dialogues),
        *(item.id for item in audio_clips),
        *(item.id for item in compositions),
    }
    project_tasks = list(
        (
            await session.scalars(
                select(AITask).where(
                    AITask.project_id == chapter.project_id,
                    AITask.tenant_id == chapter.tenant_id,
                    AITask.user_id == chapter.user_id,
                )
            )
        ).all()
    )
    tasks = [
        task
        for task in project_tasks
        if task.id in child_task_ids
        or _contains_resource(task.request_payload or {}, resource_ids)
        or _contains_resource(task.result_payload or {}, resource_ids)
    ]
    active_tasks = [
        task for task in tasks if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
    ]
    if active_tasks:
        raise ChapterHasActiveTasksError(
            f"章节仍有 {len(active_tasks)} 个排队中或进行中的任务，请先停止任务后再操作"
        )

    project_files = list(
        (
            await session.scalars(
                select(ProjectFile).where(
                    ProjectFile.project_id == chapter.project_id,
                    ProjectFile.tenant_id == chapter.tenant_id,
                    ProjectFile.user_id == chapter.user_id,
                )
            )
        ).all()
    )
    chapter_files = [
        item
        for item in project_files
        if _contains_resource(item.file_metadata or {}, resource_ids)
        and item.id != chapter.source_file_id
    ]
    memories = list(
        (
            await session.scalars(
                select(AgentMemory).where(
                    AgentMemory.project_id == chapter.project_id,
                    AgentMemory.tenant_id == chapter.tenant_id,
                    AgentMemory.user_id == chapter.user_id,
                )
            )
        ).all()
    )
    chapter_memories = [
        item for item in memories if _contains_resource(item.memory_metadata or {}, resource_ids)
    ]

    storage_paths = _collect_media_paths(
        [
            *(asset.media_url for asset in assets),
            *(_reference_audio_url(asset.asset_metadata) for asset in assets),
            *(item.media_url for item in video_clips),
            *(item.media_url for item in audio_clips),
            *(item.output_url for item in compositions),
        ]
    )
    revisions = list(
        (
            await session.scalars(
                select(AssetRevision).where(AssetRevision.asset_id.in_(asset_ids))
            )
        ).all()
    )
    storage_paths.update(_collect_media_paths(item.media_url for item in revisions))
    storage_paths.update(
        _collect_media_paths(_reference_audio_url(item.asset_metadata) for item in revisions)
    )
    storage_paths.update(str(item.storage_path) for item in chapter_files if item.storage_path)

    chapter.active_script_version_id = None
    await session.flush()
    for model in (
        DirectorWorkflowRun,
        CompositionVersion,
        AudioClip,
        DialogueVersion,
        VideoClip,
        StoryboardVersion,
        AssetExtraction,
        ScriptVersion,
        ChapterAnalysis,
    ):
        await session.execute(delete(model).where(model.chapter_id == chapter.id))
    if chapter_files:
        await session.execute(
            delete(ProjectFile).where(ProjectFile.id.in_([item.id for item in chapter_files]))
        )
    if chapter_memories:
        await session.execute(
            delete(AgentMemory).where(
                AgentMemory.id.in_([item.id for item in chapter_memories])
            )
        )
    if tasks:
        await session.execute(delete(AITask).where(AITask.id.in_([task.id for task in tasks])))
    if asset_ids:
        await session.execute(delete(Asset).where(Asset.id.in_(asset_ids)))

    if delete_chapter:
        await session.delete(chapter)
    else:
        chapter.status = ChapterStatus.UNINITIALIZED
        chapter.active_script_version_id = None
    await session.flush()

    storage_paths.difference_update(await _surviving_storage_paths(session))
    return ChapterCleanupResult(
        storage_paths=storage_paths,
        deleted_asset_ids=asset_ids,
        deleted_task_ids={task.id for task in tasks},
    )
