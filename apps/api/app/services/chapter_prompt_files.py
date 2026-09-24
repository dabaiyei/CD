"""Stable, editable chapter documents backed by the current storyboard rows.

Domain revisions remain available for task/media foreign keys. They are not new
workspace files: each chapter exposes exactly one board and one prompt document.
"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.db.models import Chapter, ProjectFile, ProjectFileKind, StoryboardShot, StoryboardVersion, User
from app.domain.schemas import StoryboardShotCreate, StoryboardShotUpdate


def visible_prompt_files():
    return ProjectFile.file_metadata["superseded_by"].as_string().is_(None)


def is_prompt_file(item: ProjectFile) -> bool:
    return (item.file_metadata or {}).get("canonical_document") in {"storyboard", "video_prompts"}


async def sync_chapter_prompt_files(
    session: AsyncSession,
    board: StoryboardVersion,
    *,
    metadata: dict | None = None,
) -> tuple[ProjectFile, ProjectFile] | None:
    if not board.is_active:
        return None
    await session.flush()
    chapter = await session.scalar(
        select(Chapter)
        .where(
            Chapter.id == board.chapter_id,
            Chapter.project_id == board.project_id,
            Chapter.tenant_id == board.tenant_id,
            Chapter.user_id == board.user_id,
        )
        .with_for_update()
    )
    if chapter is None:
        raise HTTPException(404, "章节不存在")
    shots = list(
        (
            await session.scalars(
                select(StoryboardShot)
                .where(
                    StoryboardShot.storyboard_version_id == board.id,
                    StoryboardShot.tenant_id == board.tenant_id,
                    StoryboardShot.user_id == board.user_id,
                )
                .order_by(StoryboardShot.order_index)
            )
        ).all()
    )
    extras = {row.get("order_index"): row for row in (board.content or [])}
    rows = []
    for shot in shots:
        row = dict(extras.get(shot.order_index, {}))
        row.update(
            {key: getattr(shot, key) for key in StoryboardShotCreate.model_fields if hasattr(shot, key)}
        )
        row.update(
            shot_id=shot.id, order_index=shot.order_index, duration_seconds=float(shot.duration_seconds)
        )
        rows.append(row)
    # Keep the structured board in agreement with manual/per-shot updates too.
    board.content = [{key: value for key, value in row.items() if key != "shot_id"} for row in rows]
    files = list(
        (
            await session.scalars(
                select(ProjectFile)
                .options(defer(ProjectFile.content))
                .where(
                    ProjectFile.project_id == board.project_id,
                    ProjectFile.tenant_id == board.tenant_id,
                    ProjectFile.user_id == board.user_id,
                    ProjectFile.file_metadata["chapter_id"].as_string() == chapter.id,
                )
                .order_by(ProjectFile.created_at, ProjectFile.id)
            )
        ).all()
    )
    documents = []
    for kind, suffix in (("storyboard", "分镜表"), ("video_prompts", "视频提示词")):
        candidates = [
            item
            for item in files
            if (
                (item.file_metadata or {}).get("canonical_document") == kind
                or (
                    kind == "storyboard"
                    and item.kind == ProjectFileKind.STORYBOARD
                    and (item.file_metadata or {}).get("storyboard_version_id")
                    and item.editable
                    and not item.storage_path
                )
            )
            and not (item.file_metadata or {}).get("superseded_by")
        ]
        canonical = next((item for item in candidates if is_prompt_file(item)), None)
        canonical = canonical or (candidates[0] if candidates else None)
        if canonical is None:
            canonical = ProjectFile(
                id=str(uuid5(NAMESPACE_URL, f"cineforge:chapter:{chapter.id}:{kind}")),
                tenant_id=board.tenant_id,
                user_id=board.user_id,
                project_id=board.project_id,
                kind=ProjectFileKind.STORYBOARD if kind == "storyboard" else ProjectFileKind.OTHER,
            )
            session.add(canonical)
        document_rows = (
            [{key: value for key, value in row.items() if key != "video_prompt"} for row in rows]
            if kind == "storyboard"
            else [
                {key: row[key] for key in ("shot_id", "order_index", "title", "video_prompt")} for row in rows
            ]
        )
        canonical.name = f"{chapter.title[:210]}-{suffix}.json"
        canonical.mime_type = "application/json"
        canonical.content = json.dumps({"shots": document_rows}, ensure_ascii=False, indent=2)
        canonical.size_bytes = len(canonical.content.encode("utf-8"))
        canonical.editable = True
        canonical.file_metadata = {
            **(canonical.file_metadata or {}),
            **(metadata or {}),
            "chapter_id": chapter.id,
            "script_version_id": board.script_version_id,
            "storyboard_version_id": board.id,
            "canonical_document": kind,
        }
        for obsolete in candidates:
            if obsolete.id != canonical.id:
                # Retain old contents for recovery, but never feed them to AI again.
                obsolete.file_metadata = {**(obsolete.file_metadata or {}), "superseded_by": canonical.id}
                obsolete.editable = False
        documents.append(canonical)
    await session.flush()
    return documents[0], documents[1]


async def ensure_project_prompt_files(
    session: AsyncSession,
    *,
    project_id: str,
    user: User,
    chapter_id: str | None = None,
) -> None:
    """Idempotently migrate legacy chapter files when a project is opened."""
    boards = list(
        (
            await session.scalars(
                select(StoryboardVersion)
                .options(defer(StoryboardVersion.content))
                .where(
                    StoryboardVersion.project_id == project_id,
                    StoryboardVersion.user_id == user.id,
                    StoryboardVersion.tenant_id == user.tenant_id,
                    StoryboardVersion.is_active.is_(True),
                    *([StoryboardVersion.chapter_id == chapter_id] if chapter_id else []),
                )
            )
        ).all()
    )
    files = list(
        (
            await session.scalars(
                select(ProjectFile)
                .options(defer(ProjectFile.content))
                .where(
                    ProjectFile.project_id == project_id,
                    ProjectFile.user_id == user.id,
                    ProjectFile.tenant_id == user.tenant_id,
                    visible_prompt_files(),
                    ProjectFile.file_metadata["chapter_id"].as_string().is_not(None),
                )
            )
        ).all()
    )
    for board in boards:
        chapter_files = [
            item for item in files if (item.file_metadata or {}).get("chapter_id") == board.chapter_id
        ]
        canonical = [
            item
            for item in chapter_files
            if is_prompt_file(item) and item.file_metadata.get("storyboard_version_id") == board.id
        ]
        legacy = any(
            item.kind == ProjectFileKind.STORYBOARD
            and not is_prompt_file(item)
            and (item.file_metadata or {}).get("storyboard_version_id")
            for item in chapter_files
        )
        if len(canonical) != 2 or legacy:
            await session.refresh(board, attribute_names=["content"])
            await sync_chapter_prompt_files(session, board)


async def apply_prompt_file_edit(session: AsyncSession, item: ProjectFile, content: str, user: User) -> None:
    """Apply an Edit atomically to existing shot IDs; absent rows are not deletions."""
    from app.api.routes.storyboards import apply_storyboard_shot_update

    meta = item.file_metadata or {}
    board = await session.scalar(
        select(StoryboardVersion)
        .where(
            StoryboardVersion.id == meta.get("storyboard_version_id"),
            StoryboardVersion.project_id == item.project_id,
            StoryboardVersion.user_id == user.id,
            StoryboardVersion.tenant_id == user.tenant_id,
            StoryboardVersion.is_active.is_(True),
        )
        .with_for_update()
    )
    if board is None or meta.get("superseded_by"):
        raise HTTPException(409, "分镜已更新，请重新读取当前文件后修改")
    try:
        data = json.loads(content)
        rows = data["shots"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("shots 必须为非空数组")
        old_rows = {row["shot_id"]: row for row in json.loads(item.content)["shots"]}
        by_order = {row["order_index"]: row for row in old_rows.values()}
        changes = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("镜头必须为对象")
            old = (
                old_rows.get(row.get("shot_id"))
                if row.get("shot_id")
                else by_order.get(row.get("order_index"))
            )
            if old is None or old["shot_id"] in seen:
                raise ValueError("镜头不存在或重复；请在当前文件中按 shot_id 修改")
            seen.add(old["shot_id"])
            delta = {key: value for key, value in row.items() if value != old.get(key)}
            allowed = (
                {"video_prompt"}
                if meta["canonical_document"] == "video_prompts"
                else set(StoryboardShotUpdate.model_fields) - {"video_prompt"}
            )
            if set(delta) - allowed:
                raise ValueError("不可修改镜头标识或未支持的字段：" + ", ".join(sorted(set(delta) - allowed)))
            if delta:
                if any(
                    value is None
                    and key not in {"combat_plan", "emotion_plan", "frame_layout", "reference_image_url"}
                    for key, value in delta.items()
                ):
                    raise ValueError("镜头字段不能为 null")
                changes.append((old["shot_id"], StoryboardShotUpdate.model_validate(delta)))
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, f"提示词文件格式无效：{exc}") from exc
    # Every update shares the caller's transaction/savepoint. A failed second row
    # must not leave the first one committed or overwrite generated media.
    for shot_id, payload in changes:
        await apply_storyboard_shot_update(
            item.project_id, board.chapter_id, board.id, shot_id, payload, user, session
        )
    await sync_chapter_prompt_files(session, board)
