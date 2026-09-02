from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import SecretBox
from app.db.models import (
    AgentKind,
    AgentProfile,
    AIModel,
    AITask,
    Asset,
    AssetExtraction,
    AssetExtractionItem,
    AssetScope,
    AssetStatus,
    AssetType,
    ModelType,
    Project,
    Provider,
    TaskEvent,
    User,
)
from app.services.billing import resolve_task_pricing
from app.services.image_model_routing import resolve_image_model
from app.services.object_storage import materialize_media_file, object_key_from_media_url
from app.services.task_submission import active_tasks, create_queued_task

AssetSelectionTarget = Literal["listed_assets", "active_chapter_extraction", "all_project_assets"]


async def resolve_general_agent_text_model(
    session: AsyncSession,
    tenant_id: str,
) -> tuple[AgentProfile, AIModel, Provider, str]:
    agent = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.kind == AgentKind.GENERAL,
            AgentProfile.enabled.is_(True),
        )
    )
    if agent is None:
        raise HTTPException(status_code=409, detail="管理员尚未配置可用的通用 AI")
    model = await session.get(AIModel, agent.text_model_id) if agent.text_model_id else None
    if model is None:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.tenant_id == tenant_id,
                AIModel.model_type == ModelType.TEXT,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if (
        model is None
        or model.tenant_id != tenant_id
        or model.model_type != ModelType.TEXT
        or not model.enabled
    ):
        raise HTTPException(status_code=409, detail="当前 Agent 没有可用的文本模型")
    provider = await session.get(Provider, model.provider_id)
    if provider is None or provider.tenant_id != tenant_id or not provider.enabled:
        raise HTTPException(status_code=409, detail="文本模型所属平台当前不可用")
    api_key = SecretBox().decrypt(provider.encrypted_api_key)
    if not api_key:
        raise HTTPException(status_code=409, detail="文本模型平台尚未配置 API Key")
    return agent, model, provider, api_key


async def project_assets_by_ids(
    session: AsyncSession,
    *,
    user: User,
    project_id: str,
    asset_ids: list[str],
) -> list[Asset]:
    assets = list(
        (
            await session.scalars(
                select(Asset).where(
                    Asset.id.in_(asset_ids),
                    Asset.tenant_id == user.tenant_id,
                    Asset.user_id == user.id,
                    Asset.project_id == project_id,
                    Asset.scope == AssetScope.PROJECT,
                )
            )
        ).all()
    )
    by_id = {asset.id: asset for asset in assets}
    ordered = [by_id[asset_id] for asset_id in asset_ids if asset_id in by_id]
    if len(ordered) != len(asset_ids):
        raise HTTPException(status_code=422, detail="选择中包含不可用的项目资产")
    return ordered


async def project_assets_for_generation(
    session: AsyncSession,
    *,
    user: User,
    project: Project,
    target: AssetSelectionTarget = "listed_assets",
    asset_ids: list[str] | None = None,
    asset_names: list[str] | None = None,
    chapter_id: str | None = None,
) -> list[Asset]:
    asset_ids = list(dict.fromkeys(asset_ids or []))
    asset_names = list(dict.fromkeys(name.strip() for name in asset_names or [] if name.strip()))
    if target == "listed_assets":
        if asset_ids:
            return await project_assets_by_ids(
                session,
                user=user,
                project_id=project.id,
                asset_ids=asset_ids,
            )
        if not asset_names:
            raise HTTPException(status_code=422, detail="请指定需要处理的资产")
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(
                        Asset.tenant_id == user.tenant_id,
                        Asset.user_id == user.id,
                        Asset.project_id == project.id,
                        Asset.scope == AssetScope.PROJECT,
                        Asset.name.in_(asset_names),
                    )
                    .order_by(Asset.asset_type, Asset.name)
                )
            ).all()
        )
        found = {asset.name for asset in assets}
        missing = [name for name in asset_names if name not in found]
        if missing:
            raise HTTPException(status_code=422, detail=f"资产不存在：{'、'.join(missing[:8])}")
        return assets
    if target == "active_chapter_extraction":
        if not chapter_id:
            raise HTTPException(status_code=422, detail="当前对话没有绑定章节，无法定位章节资产")
        extraction = await session.scalar(
            select(AssetExtraction)
            .where(
                AssetExtraction.tenant_id == user.tenant_id,
                AssetExtraction.user_id == user.id,
                AssetExtraction.project_id == project.id,
                AssetExtraction.chapter_id == chapter_id,
                AssetExtraction.is_active.is_(True),
            )
            .order_by(AssetExtraction.version.desc())
            .limit(1)
        )
        if extraction is None:
            raise HTTPException(status_code=409, detail="当前章节还没有生效的资产提取版本")
        return list(
            (
                await session.scalars(
                    select(Asset)
                    .join(AssetExtractionItem, AssetExtractionItem.asset_id == Asset.id)
                    .where(
                        AssetExtractionItem.extraction_id == extraction.id,
                        Asset.user_id == user.id,
                    )
                    .order_by(Asset.asset_type, Asset.name)
                )
            ).all()
        )
    return list(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.tenant_id == user.tenant_id,
                    Asset.user_id == user.id,
                    Asset.project_id == project.id,
                    Asset.scope == AssetScope.PROJECT,
                )
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )


async def queue_asset_prompt_generation_task(
    session: AsyncSession,
    *,
    user: User,
    project: Project,
    assets: list[Asset],
    only_missing_prompt: bool = False,
    auto_queue_images_after_prompt: bool = False,
) -> tuple[AITask, TaskEvent, list[Asset]]:
    selected_assets = (
        [asset for asset in assets if not asset.generation_prompt.strip()]
        if only_missing_prompt
        else assets
    )
    if not selected_assets:
        raise HTTPException(status_code=409, detail="所选资产都已有生图提示词")
    if len(selected_assets) > 100:
        raise HTTPException(status_code=422, detail="单次最多生成 100 个资产提示词")
    selected_ids = {asset.id for asset in selected_assets}
    for pending in await active_tasks(
        session,
        project_id=project.id,
        task_type="asset_prompt_generation",
    ):
        if selected_ids.intersection(pending.request_payload.get("asset_ids") or []):
            raise HTTPException(status_code=409, detail="部分资产已有提示词任务正在处理")

    agent, model, _provider, _api_key = await resolve_general_agent_text_model(session, user.tenant_id)
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="asset_prompt_generation",
        quantity=len(selected_assets),
    )
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project.id,
        task_type="asset_prompt_generation",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "asset_ids": [asset.id for asset in selected_assets],
            "asset_versions": {asset.id: asset.version for asset in selected_assets},
            "agent_profile_id": agent.id,
            "pricing": pricing.as_payload(),
            "auto_queue_images_after_prompt": auto_queue_images_after_prompt,
        },
        message=f"{len(selected_assets)} 个资产提示词生成",
    )
    return task, event, selected_assets


async def asset_has_ready_image(asset: Asset) -> bool:
    if asset.status != AssetStatus.READY:
        return False
    storage_key = object_key_from_media_url(asset.media_url)
    if not storage_key:
        return False
    try:
        await materialize_media_file(storage_key)
    except (FileNotFoundError, ValueError):
        return False
    return True


async def queue_asset_image_generation_tasks(
    session: AsyncSession,
    *,
    user: User,
    project: Project,
    assets: list[Asset],
    only_missing_image: bool = False,
) -> list[tuple[AITask, TaskEvent, Asset]]:
    selected_assets = []
    for asset in assets:
        if only_missing_image and await asset_has_ready_image(asset):
            continue
        selected_assets.append(asset)
    if not selected_assets:
        raise HTTPException(status_code=409, detail="所选资产都已有可用图片")
    if len(selected_assets) > 50:
        raise HTTPException(status_code=422, detail="单次最多生成 50 个资产")
    if any(not asset.generation_prompt.strip() for asset in selected_assets):
        raise HTTPException(status_code=409, detail="只有提示词就绪的资产才能生图")
    if any(asset.asset_type == AssetType.AUDIO for asset in selected_assets):
        raise HTTPException(status_code=409, detail="音频资产不能执行生图任务")

    image_model = await resolve_image_model(
        session,
        tenant_id=user.tenant_id,
        resolution=project.image_resolution,
        fallback_model_id=project.image_model_id,
    )
    if image_model is None:
        raise HTTPException(
            status_code=409,
            detail=f"管理员尚未为 {project.image_resolution} 配置可用的图片模型",
        )

    selected_ids = {asset.id for asset in selected_assets}
    for pending in await active_tasks(
        session,
        project_id=project.id,
        task_type="asset_image_generation",
    ):
        if pending.request_payload.get("asset_id") in selected_ids:
            raise HTTPException(status_code=409, detail="部分资产已有生图任务正在处理")

    queued: list[tuple[AITask, TaskEvent, Asset]] = []
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="asset_image_generation",
    )
    for asset in selected_assets:
        task, event = await create_queued_task(
            session,
            user=user,
            project_id=project.id,
            task_type="asset_image_generation",
            model_id=image_model.id,
            cost=pricing.total_cost,
            request_payload={
                "asset_id": asset.id,
                "asset_version": asset.version,
                "previous_asset_status": asset.status.value,
                "image_resolution": project.image_resolution,
                "aspect_ratio": project.aspect_ratio,
                "pricing": pricing.as_payload(),
            },
            message=f"资产“{asset.name}”图片生成",
        )
        asset.status = AssetStatus.GENERATING
        queued.append((task, event, asset))
    return queued
