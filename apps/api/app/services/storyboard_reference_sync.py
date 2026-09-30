"""Explicitly refresh a board's asset snapshots without regenerating media."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, AssetRevision, AssetType, StoryboardShot, StoryboardVersion
from app.services.asset_identity import asset_name_key
from app.services.chapter_prompt_files import sync_chapter_prompt_files


async def historical_asset_names(session: AsyncSession, board: StoryboardVersion) -> dict[str, set[str]]:
    """Recover identity from structured snapshots, never from scene prose."""
    names: dict[str, set[str]] = {}
    contents = (
        await session.scalars(
            select(StoryboardVersion.content).where(
                StoryboardVersion.project_id == board.project_id,
                StoryboardVersion.tenant_id == board.tenant_id,
                StoryboardVersion.user_id == board.user_id,
            )
        )
    ).all()
    for content in contents:
        for row in content or []:
            pairs = [
                (b.get("id"), b.get("name")) for b in row.get("asset_bindings", []) if isinstance(b, dict)
            ]
            ids, labels = row.get("asset_ids", []), row.get("asset_names", [])
            if len(ids) == len(labels):
                pairs.extend(zip(ids, labels, strict=True))
            for aid, name in pairs:
                if isinstance(aid, str) and isinstance(name, str) and name.strip():
                    names.setdefault(aid, set()).add(asset_name_key(name))
    return names


async def remember_deleted_asset(session: AsyncSession, asset: Asset) -> None:
    """Retain a name tombstone in boards before cascade-deleting asset revisions."""
    if not asset.project_id:
        return
    boards = (
        await session.scalars(
            select(StoryboardVersion).where(
                StoryboardVersion.project_id == asset.project_id,
                StoryboardVersion.tenant_id == asset.tenant_id,
                StoryboardVersion.user_id == asset.user_id,
            )
        )
    ).all()
    for board in boards:
        rows = []
        changed = False
        for source in board.content or []:
            row = dict(source)
            if asset.id in row.get("asset_ids", []):
                bindings = [b for b in row.get("asset_bindings", []) if b.get("id") != asset.id]
                bindings.append({"id": asset.id, "name": asset.name})
                row["asset_bindings"] = bindings
                changed = True
            rows.append(row)
        if changed:
            board.content = rows


async def sync_asset_images(session: AsyncSession, board: StoryboardVersion) -> dict:
    shots = list(
        (
            await session.scalars(
                select(StoryboardShot).where(
                    StoryboardShot.storyboard_version_id == board.id,
                    StoryboardShot.user_id == board.user_id,
                    StoryboardShot.tenant_id == board.tenant_id,
                )
            )
        ).all()
    )
    assets = {
        asset.id: asset
        for asset in (
            await session.scalars(
                select(Asset).where(
                    Asset.project_id == board.project_id,
                    Asset.user_id == board.user_id,
                    Asset.tenant_id == board.tenant_id,
                    Asset.asset_type != AssetType.AUDIO,
                )
            )
        ).all()
    }
    revisions = (
        await session.execute(
            select(AssetRevision.asset_id, AssetRevision.media_url).where(
                AssetRevision.asset_id.in_(assets),
                AssetRevision.tenant_id == board.tenant_id,
            )
        )
    ).all()
    old_urls: dict[str, set[str]] = {
        aid: {a.media_url} if a.media_url else set() for aid, a in assets.items()
    }
    for aid, url in revisions:
        if url:
            old_urls[aid].add(url)
    updated, skipped, repaired, unresolved = 0, [], 0, []
    historical = await historical_asset_names(session, board)
    names: dict[str, list[Asset]] = {}
    for asset in assets.values():
        names.setdefault(asset_name_key(asset.name), []).append(asset)
    for shot in shots:
        previous_ids = list(shot.asset_ids or [])
        missing = [aid for aid in previous_ids if aid not in assets]
        bindings_changed = False
        if missing:
            replacements = {}
            for aid in missing:
                labels = historical.get(aid, set())
                candidates = names.get(next(iter(labels)), []) if len(labels) == 1 else []
                if len(candidates) == 1:
                    replacements[aid] = candidates[0].id
            if replacements:
                shot.asset_ids = list(dict.fromkeys(replacements.get(aid, aid) for aid in previous_ids))
                bindings_changed = True
                repaired += len(replacements)
            if len(replacements) < len(missing):
                unresolved.append(shot.order_index)
        bound = [assets[aid] for aid in (shot.asset_ids or []) if aid in assets]
        # Keep the identity of an existing asset reference, even when it was not
        # the first binding. Generated/custom frames use the first bound image.
        source = next((a for a in bound if shot.reference_image_url in old_urls[a.id]), None)
        if source is None:
            source = next((a for a in bound if a.media_url), None)
        if source is None or not source.media_url:
            skipped.append(shot.order_index)
            if bindings_changed:
                shot.version += 1
                updated += 1
            continue
        if shot.reference_image_url != source.media_url or bindings_changed:
            shot.reference_image_url = source.media_url
            shot.version += 1
            updated += 1
    if updated:
        await sync_chapter_prompt_files(session, board)
    return {
        "total": len(shots),
        "updated": updated,
        "skipped": skipped,
        "assets": len(assets),
        "repaired_bindings": repaired,
        "unresolved": unresolved,
    }
