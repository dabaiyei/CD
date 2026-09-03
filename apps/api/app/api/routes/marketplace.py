from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import (
    Asset,
    AssetScope,
    AssetStatus,
    AssetType,
    MarketplaceAcquisition,
    MarketplaceListing,
    MarketplaceResourceType,
    User,
    UserSkill,
    UserSkillStage,
    UserTemplate,
)
from app.db.session import get_session
from app.domain.schemas import (
    AssetPublic,
    MarketplaceAcquisitionResult,
    MarketplaceListingPublic,
    MarketplacePage,
    MarketplacePublishRequest,
    UserTemplateCreate,
    UserTemplatePublic,
    UserTemplateUpdate,
)
from app.services.asset_revisions import snapshot_asset_revision

router = APIRouter(tags=["marketplace"])


async def unique_owned_name(
    session: AsyncSession,
    *,
    model: type[UserSkill] | type[UserTemplate],
    user: User,
    requested: str,
    max_length: int = 120,
) -> str:
    base = requested.strip()[:max_length] or "广场资源"
    existing = set(
        (
            await session.scalars(
                select(func.lower(model.name)).where(
                    model.tenant_id == user.tenant_id,
                    model.user_id == user.id,
                )
            )
        ).all()
    )
    if base.casefold() not in existing:
        return base
    for index in range(2, 1000):
        suffix = f"（广场 {index}）"
        candidate = f"{base[: max_length - len(suffix)]}{suffix}"
        if candidate.casefold() not in existing:
            return candidate
    raise HTTPException(status_code=409, detail="本地同名资源过多，请先整理个人资源")


async def owned_template(session: AsyncSession, template_id: str, user: User) -> UserTemplate:
    template = await session.get(UserTemplate, template_id)
    if template is None or template.tenant_id != user.tenant_id or template.user_id != user.id:
        raise HTTPException(status_code=404, detail="个人模板不存在")
    return template


async def ensure_unique_template_name(
    session: AsyncSession,
    *,
    user: User,
    name: str,
    exclude_id: str | None = None,
) -> None:
    conditions = [
        UserTemplate.tenant_id == user.tenant_id,
        UserTemplate.user_id == user.id,
        func.lower(UserTemplate.name) == name.casefold(),
    ]
    if exclude_id:
        conditions.append(UserTemplate.id != exclude_id)
    if await session.scalar(select(UserTemplate.id).where(*conditions)):
        raise HTTPException(status_code=409, detail="已有同名个人模板")


def listing_public(
    listing: MarketplaceListing,
    publisher: User,
    acquisition: MarketplaceAcquisition | None,
    user: User,
) -> MarketplaceListingPublic:
    return MarketplaceListingPublic(
        id=listing.id,
        resource_type=listing.resource_type,
        title=listing.title,
        description=listing.description,
        category=listing.category,
        tags=list(listing.tags or []),
        cover_url=listing.cover_url,
        payload=dict(listing.payload or {}),
        version=listing.version,
        download_count=listing.download_count,
        publisher_name=publisher.display_name,
        publisher_avatar_url=publisher.avatar_url,
        owned_by_me=listing.publisher_user_id == user.id,
        acquired=acquisition is not None,
        has_update=bool(acquisition and acquisition.listing_version < listing.version),
        target_id=acquisition.target_id if acquisition else None,
        published_at=listing.published_at,
        updated_at=listing.updated_at,
    )


@router.get("/marketplace/material-sources", response_model=list[AssetPublic])
async def list_material_sources(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Asset]:
    return list(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.tenant_id == user.tenant_id,
                    Asset.user_id == user.id,
                )
                .order_by(Asset.scope.desc(), Asset.updated_at.desc(), Asset.id.desc())
            )
        ).all()
    )


@router.get("/marketplace/{resource_type}", response_model=MarketplacePage)
async def list_marketplace(
    resource_type: MarketplaceResourceType,
    search: str = Query(default="", max_length=120),
    category: str | None = Query(default=None, max_length=80),
    sort: str = Query(default="newest", pattern="^(newest|popular)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=48, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MarketplacePage:
    acquisition_join = and_(
        MarketplaceAcquisition.listing_id == MarketplaceListing.id,
        MarketplaceAcquisition.user_id == user.id,
    )
    filters = [
        MarketplaceListing.resource_type == resource_type,
        MarketplaceListing.published.is_(True),
    ]
    keyword = search.strip().casefold()
    if keyword:
        filters.append(
            or_(
                func.lower(MarketplaceListing.title).contains(keyword),
                func.lower(MarketplaceListing.description).contains(keyword),
                func.lower(MarketplaceListing.category).contains(keyword),
            )
        )
    if category:
        filters.append(MarketplaceListing.category == category)
    order = (
        (MarketplaceListing.download_count.desc(), MarketplaceListing.updated_at.desc())
        if sort == "popular"
        else (MarketplaceListing.updated_at.desc(), MarketplaceListing.id.desc())
    )
    rows = (
        await session.execute(
            select(MarketplaceListing, User, MarketplaceAcquisition)
            .join(User, User.id == MarketplaceListing.publisher_user_id)
            .outerjoin(MarketplaceAcquisition, acquisition_join)
            .where(*filters)
            .order_by(*order)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    total = int(
        await session.scalar(select(func.count(MarketplaceListing.id)).where(*filters)) or 0
    )
    categories = list(
        (
            await session.scalars(
                select(MarketplaceListing.category)
                .where(
                    MarketplaceListing.resource_type == resource_type,
                    MarketplaceListing.published.is_(True),
                )
                .distinct()
                .order_by(MarketplaceListing.category)
            )
        ).all()
    )
    return MarketplacePage(
        items=[
            listing_public(listing, publisher, acquisition, user)
            for listing, publisher, acquisition in rows
        ],
        total=total,
        categories=categories,
    )


async def publish_snapshot(
    session: AsyncSession,
    *,
    resource_type: MarketplaceResourceType,
    source_id: str,
    title: str,
    description: str,
    category: str,
    tags: list[str],
    cover_url: str | None,
    payload: dict,
    user: User,
) -> MarketplaceListing:
    listing = await session.scalar(
        select(MarketplaceListing).where(
            MarketplaceListing.resource_type == resource_type,
            MarketplaceListing.publisher_user_id == user.id,
            MarketplaceListing.source_id == source_id,
        )
    )
    if listing is None:
        listing = MarketplaceListing(
            resource_type=resource_type,
            publisher_tenant_id=user.tenant_id,
            publisher_user_id=user.id,
            source_id=source_id,
            title=title,
            description=description,
            category=category,
            tags=tags,
            cover_url=cover_url,
            payload=payload,
            published=True,
        )
        session.add(listing)
    else:
        listing.title = title
        listing.description = description
        listing.category = category
        listing.tags = tags
        listing.cover_url = cover_url
        listing.payload = payload
        listing.version += 1
        listing.published = True
        listing.published_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(listing)
    return listing


@router.post("/marketplace/skills/{skill_id}/publish", response_model=MarketplaceListingPublic)
async def publish_skill(
    skill_id: str,
    payload: MarketplacePublishRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MarketplaceListingPublic:
    skill = await session.get(UserSkill, skill_id)
    if skill is None or skill.tenant_id != user.tenant_id or skill.user_id != user.id:
        raise HTTPException(status_code=404, detail="个人 Skill 不存在")
    listing = await publish_snapshot(
        session,
        resource_type=MarketplaceResourceType.SKILL,
        source_id=skill.id,
        title=skill.name,
        description=skill.description,
        category=payload.category,
        tags=payload.tags,
        cover_url=None,
        payload={
            "name": skill.name,
            "description": skill.description,
            "trigger_stages": list(skill.trigger_stages or []),
            "source_version": skill.version,
        },
        user=user,
    )
    return listing_public(listing, user, None, user)


@router.post(
    "/marketplace/templates/{template_id}/publish",
    response_model=MarketplaceListingPublic,
)
async def publish_template(
    template_id: str,
    payload: MarketplacePublishRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MarketplaceListingPublic:
    template = await owned_template(session, template_id, user)
    listing = await publish_snapshot(
        session,
        resource_type=MarketplaceResourceType.TEMPLATE,
        source_id=template.id,
        title=template.name,
        description=template.description,
        category=payload.category or template.category,
        tags=payload.tags,
        cover_url=None,
        payload={
            "name": template.name,
            "description": template.description,
            "category": template.category,
            "content": template.content,
            "source_version": template.version,
        },
        user=user,
    )
    return listing_public(listing, user, None, user)


@router.post(
    "/marketplace/materials/{asset_id}/publish",
    response_model=MarketplaceListingPublic,
)
async def publish_material(
    asset_id: str,
    payload: MarketplacePublishRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MarketplaceListingPublic:
    asset = await session.get(Asset, asset_id)
    if (
        asset is None
        or asset.tenant_id != user.tenant_id
        or asset.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="可分享的素材不存在")
    listing = await publish_snapshot(
        session,
        resource_type=MarketplaceResourceType.MATERIAL,
        source_id=asset.id,
        title=asset.name,
        description=asset.description,
        category=payload.category,
        tags=payload.tags,
        cover_url=asset.media_url if asset.asset_type != AssetType.AUDIO else None,
        payload={
            "name": asset.name,
            "description": asset.description,
            "asset_type": asset.asset_type.value,
            "generation_prompt": asset.generation_prompt,
            "media_url": asset.media_url,
            "status": asset.status.value,
            "asset_metadata": dict(asset.asset_metadata or {}),
            "source_version": asset.version,
        },
        user=user,
    )
    return listing_public(listing, user, None, user)


@router.delete("/marketplace/listings/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unpublish_listing(
    listing_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    listing = await session.get(MarketplaceListing, listing_id)
    if listing is None or listing.publisher_user_id != user.id:
        raise HTTPException(status_code=404, detail="广场资源不存在")
    listing.published = False
    await session.commit()


@router.post(
    "/marketplace/listings/{listing_id}/acquire",
    response_model=MarketplaceAcquisitionResult,
)
async def acquire_listing(
    listing_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MarketplaceAcquisitionResult:
    listing = await session.scalar(
        select(MarketplaceListing)
        .where(MarketplaceListing.id == listing_id, MarketplaceListing.published.is_(True))
        .with_for_update()
    )
    if listing is None:
        raise HTTPException(status_code=404, detail="广场资源不存在或已下架")
    acquisition = await session.scalar(
        select(MarketplaceAcquisition).where(
            MarketplaceAcquisition.listing_id == listing.id,
            MarketplaceAcquisition.user_id == user.id,
        )
    )
    target_created = False

    if listing.resource_type == MarketplaceResourceType.SKILL:
        target = await session.get(UserSkill, acquisition.target_id) if acquisition else None
        if target is None or target.tenant_id != user.tenant_id or target.user_id != user.id:
            target = UserSkill(
                tenant_id=user.tenant_id,
                user_id=user.id,
                name=await unique_owned_name(
                    session, model=UserSkill, user=user, requested=str(listing.payload["name"])
                ),
                description=str(listing.payload["description"]),
                trigger_stages=list(listing.payload.get("trigger_stages") or []),
                enabled=True,
            )
            session.add(target)
            await session.flush()
            target_created = True
        else:
            target.description = str(listing.payload["description"])
            target.trigger_stages = [
                UserSkillStage(value).value
                for value in list(listing.payload.get("trigger_stages") or [])
            ]
            target.version += 1
        target_type = "user_skill"
    elif listing.resource_type == MarketplaceResourceType.TEMPLATE:
        target = await session.get(UserTemplate, acquisition.target_id) if acquisition else None
        if target is None or target.tenant_id != user.tenant_id or target.user_id != user.id:
            target = UserTemplate(
                tenant_id=user.tenant_id,
                user_id=user.id,
                name=await unique_owned_name(
                    session,
                    model=UserTemplate,
                    user=user,
                    requested=str(listing.payload["name"]),
                ),
                description=str(listing.payload.get("description") or ""),
                category=str(listing.payload.get("category") or listing.category),
                content=str(listing.payload["content"]),
            )
            session.add(target)
            await session.flush()
            target_created = True
        else:
            target.description = str(listing.payload.get("description") or "")
            target.category = str(listing.payload.get("category") or listing.category)
            target.content = str(listing.payload["content"])
            target.version += 1
        target_type = "user_template"
    else:
        target = await session.get(Asset, acquisition.target_id) if acquisition else None
        if target is None or target.tenant_id != user.tenant_id or target.user_id != user.id:
            target = Asset(
                tenant_id=user.tenant_id,
                user_id=user.id,
                project_id=None,
                scope=AssetScope.GLOBAL,
                asset_type=AssetType(str(listing.payload["asset_type"])),
                name=str(listing.payload["name"])[:160],
                description=str(listing.payload.get("description") or ""),
                generation_prompt=str(listing.payload.get("generation_prompt") or ""),
                media_url=listing.payload.get("media_url"),
                status=AssetStatus(str(listing.payload.get("status") or "extracted")),
                asset_metadata={
                    **dict(listing.payload.get("asset_metadata") or {}),
                    "marketplace_listing_id": listing.id,
                    "marketplace_publisher_id": listing.publisher_user_id,
                },
            )
            session.add(target)
            await session.flush()
            target_created = True
            await snapshot_asset_revision(session, target, change_type="marketplace_copy")
        else:
            target.description = str(listing.payload.get("description") or "")
            target.generation_prompt = str(listing.payload.get("generation_prompt") or "")
            target.media_url = listing.payload.get("media_url")
            target.status = AssetStatus(str(listing.payload.get("status") or "extracted"))
            target.asset_metadata = {
                **dict(listing.payload.get("asset_metadata") or {}),
                "marketplace_listing_id": listing.id,
                "marketplace_publisher_id": listing.publisher_user_id,
            }
            target.version += 1
            await snapshot_asset_revision(session, target, change_type="marketplace_sync")
        target_type = "global_asset"

    if acquisition is None:
        acquisition = MarketplaceAcquisition(
            listing_id=listing.id,
            tenant_id=user.tenant_id,
            user_id=user.id,
            target_type=target_type,
            target_id=target.id,
            listing_version=listing.version,
        )
        session.add(acquisition)
        await session.execute(
            update(MarketplaceListing)
            .where(MarketplaceListing.id == listing.id)
            .values(download_count=MarketplaceListing.download_count + 1)
        )
    else:
        acquisition.target_type = target_type
        acquisition.target_id = target.id
        acquisition.listing_version = listing.version
    await session.commit()
    return MarketplaceAcquisitionResult(
        listing_id=listing.id,
        target_type=target_type,
        target_id=target.id,
        listing_version=listing.version,
        created=target_created,
    )


@router.get("/user-templates", response_model=list[UserTemplatePublic])
async def list_user_templates(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[UserTemplate]:
    return list(
        (
            await session.scalars(
                select(UserTemplate)
                .where(
                    UserTemplate.tenant_id == user.tenant_id,
                    UserTemplate.user_id == user.id,
                )
                .order_by(UserTemplate.updated_at.desc(), UserTemplate.id.desc())
            )
        ).all()
    )


@router.post("/user-templates", response_model=UserTemplatePublic, status_code=status.HTTP_201_CREATED)
async def create_user_template(
    payload: UserTemplateCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserTemplate:
    await ensure_unique_template_name(session, user=user, name=payload.name)
    template = UserTemplate(tenant_id=user.tenant_id, user_id=user.id, **payload.model_dump())
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@router.patch("/user-templates/{template_id}", response_model=UserTemplatePublic)
async def update_user_template(
    template_id: str,
    payload: UserTemplateUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserTemplate:
    template = await owned_template(session, template_id, user)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        await ensure_unique_template_name(
            session, user=user, name=changes["name"], exclude_id=template.id
        )
    for key, value in changes.items():
        setattr(template, key, value)
    if changes:
        template.version += 1
    await session.commit()
    await session.refresh(template)
    return template


@router.delete("/user-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_template(
    template_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    template = await owned_template(session, template_id, user)
    await session.execute(
        update(MarketplaceListing)
        .where(
            MarketplaceListing.resource_type == MarketplaceResourceType.TEMPLATE,
            MarketplaceListing.publisher_user_id == user.id,
            MarketplaceListing.source_id == template.id,
        )
        .values(published=False)
    )
    await session.delete(template)
    await session.commit()
