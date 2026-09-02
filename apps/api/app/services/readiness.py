from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIModel, ModelType

REQUIRED_DEFAULT_MODEL_TYPES = (ModelType.TEXT, ModelType.IMAGE, ModelType.VIDEO)


async def default_model_types(session: AsyncSession, tenant_id: str) -> set[ModelType]:
    cache_key = f"default-model-types:{tenant_id}"
    cached = session.info.get(cache_key)
    if isinstance(cached, set):
        return cached
    present = set(
        (
            await session.scalars(
                select(AIModel.model_type).where(
                    AIModel.tenant_id == tenant_id,
                    AIModel.enabled.is_(True),
                    AIModel.is_default.is_(True),
                )
            )
        ).all()
    )
    session.info[cache_key] = present
    return present


async def require_core_models_ready(session: AsyncSession, tenant_id: str) -> None:
    present = await default_model_types(session, tenant_id)
    missing = [item.value for item in REQUIRED_DEFAULT_MODEL_TYPES if item not in present]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"系统创作能力尚未完成配置，缺少默认模型：{', '.join(missing)}",
        )
