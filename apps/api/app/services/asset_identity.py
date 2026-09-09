"""Conservative identity matching for project extraction, never across owners."""

import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, AssetScope


def asset_name_key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


async def extraction_asset_catalog(
    session: AsyncSession, project_id: str, tenant_id: str, user_id: str
) -> list[Asset]:
    return list(
        await session.scalars(
            select(Asset)
            .where(
                Asset.project_id == project_id,
                Asset.tenant_id == tenant_id,
                Asset.user_id == user_id,
                Asset.scope == AssetScope.PROJECT,
            )
            .order_by(Asset.created_at, Asset.id)
        )
    )


def reusable_asset(assets: list[Asset], asset_type: str, name: str, parent_id: str | None) -> Asset | None:
    matches = [
        asset
        for asset in assets
        if (
            asset.asset_type.value == asset_type
            and asset.parent_asset_id == parent_id
            and asset_name_key(asset.name) == asset_name_key(name)
        )
    ]
    # Keep the established visual identity when older extraction runs left duplicates.
    return next((asset for asset in matches if asset.media_url), matches[0] if matches else None)
