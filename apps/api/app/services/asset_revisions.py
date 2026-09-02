from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, AssetRevision


async def snapshot_asset_revision(
    session: AsyncSession,
    asset: Asset,
    *,
    change_type: str,
    source_task_id: str | None = None,
    source_revision_id: str | None = None,
) -> AssetRevision:
    if asset.id is None:
        await session.flush()
    revision = AssetRevision(
        tenant_id=asset.tenant_id,
        asset_id=asset.id,
        version=asset.version,
        change_type=change_type,
        source_task_id=source_task_id,
        source_revision_id=source_revision_id,
        parent_asset_id=asset.parent_asset_id,
        name=asset.name,
        description=asset.description,
        generation_prompt=asset.generation_prompt,
        media_url=asset.media_url,
        status=asset.status,
        asset_metadata=dict(asset.asset_metadata or {}),
    )
    session.add(revision)
    return revision
