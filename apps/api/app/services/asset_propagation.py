"""Keep storyboards, prompts and rendered videos in step with asset versions.

A shot snapshots the asset image it was built from in ``reference_image_url``,
and a rendered clip is bound to the reference it was generated with. Switching an
asset version, replacing it from another library or regenerating its image
therefore has to reach the board too -- otherwise every later step (derivative
images, storyboard first frames, video prompts, rendered clips) keeps using the
previous look while the library claims the new one is active.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, AssetScope, StoryboardShot, StoryboardVersion, VideoClip
from app.services.composition import invalidate_compositions


async def mark_asset_image_version(session: AsyncSession, asset: Asset) -> None:
    """Record which parent version an asset image was produced from.

    A derivative drawn from the parent's previous look is otherwise
    indistinguishable from a fresh one, so the staleness check below could never
    tell the user which derivations still need to be redrawn.
    """
    metadata = dict(asset.asset_metadata or {})
    metadata.pop("stale_parent_version", None)
    if asset.parent_asset_id:
        parent = await session.get(Asset, asset.parent_asset_id)
        if parent is not None:
            metadata["parent_reference_version"] = parent.version
    else:
        metadata.pop("parent_reference_version", None)
    asset.asset_metadata = metadata


async def _shots_using_asset(session: AsyncSession, asset: Asset):
    if asset.scope != AssetScope.PROJECT or not asset.project_id:
        return []
    # Only the active board feeds production; inactive boards stay as the
    # historical snapshot of the look they were built against.
    rows = (
        await session.execute(
            select(
                StoryboardShot.id,
                StoryboardShot.asset_ids,
                StoryboardShot.chapter_id,
            )
            .join(StoryboardVersion, StoryboardVersion.id == StoryboardShot.storyboard_version_id)
            .where(
                StoryboardShot.project_id == asset.project_id,
                StoryboardVersion.is_active.is_(True),
            )
        )
    ).all()
    return [row for row in rows if asset.id in (row.asset_ids or [])]


async def refresh_active_board_references(session: AsyncSession, chapter_id: str) -> int:
    """Re-point an active board's shot frames at the current asset images.

    Boards built before an asset switched keep the old file in the shot, so the
    look has to be re-read when that board becomes the one production uses.
    """
    shots = (
        await session.scalars(
            select(StoryboardShot).where(StoryboardShot.chapter_id == chapter_id)
        )
    ).all()
    if not shots:
        return 0
    asset_ids = {asset_id for shot in shots for asset_id in (shot.asset_ids or [])}
    if not asset_ids:
        return 0
    live_media = dict(
        (
            await session.execute(
                select(Asset.id, Asset.media_url).where(Asset.id.in_(asset_ids))
            )
        ).all()
    )
    touched = 0
    for shot in shots:
        current = next(
            (live_media.get(asset_id) for asset_id in (shot.asset_ids or []) if live_media.get(asset_id)),
            None,
        )
        if current and current != shot.reference_image_url:
            shot.reference_image_url = current
            shot.video_prompt = ""
            shot.version += 1
            touched += 1
    return touched


async def propagate_asset_change(
    session: AsyncSession,
    asset: Asset,
    *,
    reason: str,
    clear_video_prompts: bool = True,
) -> dict[str, object]:
    """Push an asset change into every board, prompt and clip that used it.

    Returns a small summary so callers can tell the user what was invalidated.
    """
    rows = await _shots_using_asset(session, asset)
    summary: dict[str, object] = {"shots": 0, "chapters": [], "videos": 0, "children": 0}

    if rows:
        shot_ids = [row.id for row in rows]
        asset_ids = {asset_id for row in rows for asset_id in (row.asset_ids or [])}
        live_media = dict(
            (
                await session.execute(
                    select(Asset.id, Asset.media_url).where(Asset.id.in_(asset_ids))
                )
            ).all()
        )
        active_video_shots = set(
            (
                await session.scalars(
                    select(VideoClip.shot_id).where(
                        VideoClip.shot_id.in_(shot_ids),
                        VideoClip.is_active.is_(True),
                    )
                )
            ).all()
        )
        shots = (
            await session.scalars(select(StoryboardShot).where(StoryboardShot.id.in_(shot_ids)))
        ).all()
        for shot in shots:
            # Same rule the board and the agent use: first asset in binding order
            # that actually has an image becomes the shot reference.
            shot.reference_image_url = next(
                (live_media.get(asset_id) for asset_id in (shot.asset_ids or []) if live_media.get(asset_id)),
                None,
            )
            if clear_video_prompts:
                shot.video_prompt = ""
            shot.version += 1
        summary["shots"] = len(shots)

        summary["videos"] = (
            await session.execute(
                update(VideoClip)
                .where(VideoClip.shot_id.in_(shot_ids), VideoClip.is_active.is_(True))
                .values(is_active=False, invalidated_reason=reason)
            )
        ).rowcount or 0

        if active_video_shots:
            from app.services.video_continuity import invalidate_descendants

            for shot_id in active_video_shots:
                await invalidate_descendants(session, shot_id, "")

        chapters = {row.chapter_id for row in rows}
        for chapter_id in chapters:
            await invalidate_compositions(session, chapter_id=chapter_id, reason=reason)
        summary["chapters"] = sorted(chapters)
        from app.services.chapter_prompt_files import sync_chapter_prompt_files
        for board_id in {shot.storyboard_version_id for shot in shots}:
            board = await session.get(StoryboardVersion, board_id)
            if board is not None:
                await sync_chapter_prompt_files(session, board)

    children = (
        await session.scalars(select(Asset).where(Asset.parent_asset_id == asset.id))
    ).all()
    for child in children:
        # Nothing to redraw while the derivative has no image of its own yet.
        if not child.media_url:
            continue
        metadata = dict(child.asset_metadata or {})
        if metadata.get("parent_reference_version") == asset.version:
            continue
        metadata["stale_parent_version"] = asset.version
        metadata["stale_parent_id"] = asset.id
        child.asset_metadata = metadata
        summary["children"] += 1
    return summary
