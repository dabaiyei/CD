from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIModel, ImageResolutionModelRoute, ModelType, Provider

IMAGE_RESOLUTIONS = ("1K", "2K", "4K")


def normalize_image_resolution(resolution: str | None) -> str | None:
    normalized = str(resolution or "").strip().upper()
    return normalized if normalized in IMAGE_RESOLUTIONS else None


async def _usable_image_model(
    session: AsyncSession,
    *,
    tenant_id: str,
    model_id: str,
) -> AIModel | None:
    model = await session.get(AIModel, model_id)
    if (
        model is None
        or model.tenant_id != tenant_id
        or model.model_type != ModelType.IMAGE
        or not model.enabled
    ):
        return None
    provider = await session.get(Provider, model.provider_id)
    if provider is None or provider.tenant_id != tenant_id or not provider.enabled:
        return None
    return model


async def resolve_image_model(
    session: AsyncSession,
    *,
    tenant_id: str,
    resolution: str | None,
    fallback_model_id: str | None = None,
) -> AIModel | None:
    normalized = normalize_image_resolution(resolution)
    if normalized is None:
        return None

    route = await session.scalar(
        select(ImageResolutionModelRoute).where(
            ImageResolutionModelRoute.tenant_id == tenant_id,
            ImageResolutionModelRoute.resolution == normalized,
        )
    )
    if route is not None:
        return await _usable_image_model(
            session,
            tenant_id=tenant_id,
            model_id=route.model_id,
        )

    if fallback_model_id:
        fallback = await _usable_image_model(
            session,
            tenant_id=tenant_id,
            model_id=fallback_model_id,
        )
        if fallback is not None:
            return fallback

    default_model_id = await session.scalar(
        select(AIModel.id).where(
            AIModel.tenant_id == tenant_id,
            AIModel.model_type == ModelType.IMAGE,
            AIModel.is_default.is_(True),
            AIModel.enabled.is_(True),
        )
    )
    if default_model_id is None:
        return None
    return await _usable_image_model(
        session,
        tenant_id=tenant_id,
        model_id=default_model_id,
    )
