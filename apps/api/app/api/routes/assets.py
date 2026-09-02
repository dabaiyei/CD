from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.agent_chat import resolve_text_model
from app.api.routes.projects import project_for_user
from app.core.config import get_settings
from app.db.models import (
    AgentKind,
    AgentProfile,
    AITask,
    Asset,
    AssetExtraction,
    AssetExtractionItem,
    AssetRevision,
    AssetScope,
    AssetStatus,
    AssetType,
    Chapter,
    ChapterStatus,
    ScriptVersion,
    User,
    UserRole,
)
from app.db.session import get_session
from app.domain.schemas import (
    AssetCreate,
    AssetExtractionCreate,
    AssetExtractionPublic,
    AssetExtractionResult,
    AssetPublic,
    AssetRevisionPublic,
    AssetSelectionRequest,
    AssetUpdate,
    TaskPublic,
)
from app.services.asset_revisions import snapshot_asset_revision
from app.services.asset_tasks import (
    project_assets_by_ids,
    queue_asset_image_generation_tasks,
    queue_asset_prompt_generation_task,
)
from app.services.billing import resolve_task_pricing
from app.services.media import (
    ALLOWED_COVER_TYPES,
    MAX_COVER_BYTES,
    InvalidCoverImage,
    save_asset_image,
)
from app.services.object_storage import delete_media_file, persist_media_file
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import active_tasks, create_queued_task

router = APIRouter(tags=["assets"])
DERIVABLE_ASSET_TYPES = {AssetType.CHARACTER, AssetType.SCENE, AssetType.PROP}


async def asset_for_tenant(session: AsyncSession, asset_id: str, user: User) -> Asset:
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="资产不存在")
    return asset


def require_asset_editor(asset: Asset, user: User) -> None:
    if asset.scope == AssetScope.GLOBAL and asset.user_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="只能修改自己创建的全局资产")


async def validate_parent(
    session: AsyncSession,
    parent_id: str | None,
    *,
    scope: AssetScope,
    project_id: str | None,
    tenant_id: str,
    asset_type: AssetType,
    child_id: str | None = None,
) -> Asset | None:
    if not parent_id:
        return None
    parent = await session.get(Asset, parent_id)
    if (
        parent is None
        or parent.id == child_id
        or parent.tenant_id != tenant_id
        or parent.scope != scope
        or parent.project_id != project_id
        or parent.parent_asset_id is not None
    ):
        raise HTTPException(status_code=422, detail="衍生资产必须绑定同一资产库中的基础资产")
    if asset_type not in DERIVABLE_ASSET_TYPES or parent.asset_type != asset_type:
        raise HTTPException(status_code=422, detail="只有同类型的人物、场景或道具可以建立衍生关系")
    return parent


def copied_asset(
    source: Asset,
    *,
    user: User,
    scope: AssetScope,
    project_id: str | None,
    parent_asset_id: str | None = None,
) -> Asset:
    return Asset(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        scope=scope,
        asset_type=source.asset_type,
        parent_asset_id=parent_asset_id,
        lineage_id=source.lineage_id,
        name=source.name,
        description=source.description,
        generation_prompt=source.generation_prompt,
        media_url=source.media_url,
        status=source.status,
        asset_metadata={**source.asset_metadata, "source_asset_id": source.id},
    )


async def copy_asset_between_libraries(
    session: AsyncSession,
    source: Asset,
    *,
    user: User,
    scope: AssetScope,
    project_id: str | None,
) -> Asset:
    destination_parent_id: str | None = None
    if source.parent_asset_id:
        source_parent = await validate_parent(
            session,
            source.parent_asset_id,
            scope=source.scope,
            project_id=source.project_id,
            tenant_id=source.tenant_id,
            asset_type=source.asset_type,
            child_id=source.id,
        )
        assert source_parent is not None
        destination_parent = await session.scalar(
            select(Asset)
            .where(
                Asset.tenant_id == user.tenant_id,
                Asset.scope == scope,
                Asset.project_id == project_id,
                Asset.parent_asset_id.is_(None),
                Asset.lineage_id == source_parent.lineage_id,
            )
            .order_by(Asset.created_at, Asset.id)
        )
        if destination_parent is None:
            destination_parent = copied_asset(
                source_parent,
                user=user,
                scope=scope,
                project_id=project_id,
            )
            session.add(destination_parent)
            await session.flush()
            await snapshot_asset_revision(session, destination_parent, change_type="library_copy")
        destination_parent_id = destination_parent.id
    copied = copied_asset(
        source,
        user=user,
        scope=scope,
        project_id=project_id,
        parent_asset_id=destination_parent_id,
    )
    session.add(copied)
    await session.flush()
    await snapshot_asset_revision(session, copied, change_type="library_copy")
    return copied


def new_asset(
    payload: AssetCreate,
    *,
    user: User,
    scope: AssetScope,
    project_id: str | None,
) -> Asset:
    return Asset(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        scope=scope,
        status=AssetStatus.PROMPT_READY if payload.generation_prompt.strip() else AssetStatus.EXTRACTED,
        **payload.model_dump(),
    )


async def general_agent(session: AsyncSession, tenant_id: str) -> AgentProfile:
    agent = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.kind == AgentKind.GENERAL,
            AgentProfile.enabled.is_(True),
        )
    )
    if agent is None:
        raise HTTPException(status_code=409, detail="管理员尚未配置可用的通用 AI")
    return agent


@router.post(
    "/projects/{project_id}/chapters/{chapter_id}/asset-extractions/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_asset_extraction(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    await project_for_user(session, project_id, user)
    chapter = await session.get(Chapter, chapter_id)
    if chapter is None or chapter.project_id != project_id or chapter.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="章节不存在")
    if not chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="提取资产前必须先选择生效剧本")
    script = await session.get(ScriptVersion, chapter.active_script_version_id)
    if script is None or script.chapter_id != chapter.id:
        raise HTTPException(status_code=409, detail="章节生效剧本不可用")

    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="chapter_asset_extraction",
    ):
        if pending.request_payload.get("chapter_id") == chapter.id:
            raise HTTPException(status_code=409, detail="该章节已有资产提取任务正在处理")

    agent = await general_agent(session, user.tenant_id)
    model, _provider, _api_key = await resolve_text_model(session, agent, user.tenant_id)
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="chapter_asset_extraction",
    )
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project_id,
        task_type="chapter_asset_extraction",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "chapter_id": chapter.id,
            "script_version_id": script.id,
            "script_version": script.version,
            "agent_profile_id": agent.id,
            "pricing": pricing.as_payload(),
        },
        message="剧本资产提取",
    )
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task


@router.post(
    "/projects/{project_id}/assets/prompts/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_asset_prompts(
    project_id: str,
    payload: AssetSelectionRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    project = await project_for_user(session, project_id, user)
    assets = await project_assets_by_ids(
        session,
        user=user,
        project_id=project_id,
        asset_ids=payload.asset_ids,
    )
    task, event, _assets = await queue_asset_prompt_generation_task(
        session,
        user=user,
        project=project,
        assets=assets,
    )
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task


@router.post(
    "/projects/{project_id}/assets/images/generate",
    response_model=list[TaskPublic],
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_asset_images(
    project_id: str,
    payload: AssetSelectionRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AITask]:
    project = await project_for_user(session, project_id, user)
    assets = await project_assets_by_ids(
        session,
        user=user,
        project_id=project_id,
        asset_ids=payload.asset_ids,
    )
    queued = await queue_asset_image_generation_tasks(
        session,
        user=user,
        project=project,
        assets=assets,
    )
    await session.commit()
    for task, event, _asset in queued:
        await session.refresh(task)
        await enqueue_task(task.id)
        await publish_task_event(task, event)
    return [item[0] for item in queued]


@router.get("/assets", response_model=list[AssetPublic])
async def list_global_assets(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Asset]:
    return list(
        (
            await session.scalars(
                select(Asset)
                .where(Asset.tenant_id == user.tenant_id, Asset.scope == AssetScope.GLOBAL)
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )


@router.post("/assets", response_model=AssetPublic, status_code=status.HTTP_201_CREATED)
async def create_global_asset(
    payload: AssetCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Asset:
    await validate_parent(
        session,
        payload.parent_asset_id,
        scope=AssetScope.GLOBAL,
        project_id=None,
        tenant_id=user.tenant_id,
        asset_type=payload.asset_type,
    )
    asset = new_asset(payload, user=user, scope=AssetScope.GLOBAL, project_id=None)
    session.add(asset)
    await session.flush()
    await snapshot_asset_revision(session, asset, change_type="manual_create")
    await session.commit()
    await session.refresh(asset)
    return asset


@router.get("/projects/{project_id}/assets", response_model=list[AssetPublic])
async def list_project_assets(
    project_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Asset]:
    await project_for_user(session, project_id, user)
    return list(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.tenant_id == user.tenant_id,
                    Asset.project_id == project_id,
                    Asset.scope == AssetScope.PROJECT,
                )
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )


@router.post(
    "/projects/{project_id}/assets",
    response_model=AssetPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_asset(
    project_id: str,
    payload: AssetCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Asset:
    await project_for_user(session, project_id, user)
    await validate_parent(
        session,
        payload.parent_asset_id,
        scope=AssetScope.PROJECT,
        project_id=project_id,
        tenant_id=user.tenant_id,
        asset_type=payload.asset_type,
    )
    asset = new_asset(payload, user=user, scope=AssetScope.PROJECT, project_id=project_id)
    session.add(asset)
    await session.flush()
    await snapshot_asset_revision(session, asset, change_type="manual_create")
    await session.commit()
    await session.refresh(asset)
    return asset


@router.patch("/assets/{asset_id}", response_model=AssetPublic)
async def update_asset(
    asset_id: str,
    payload: AssetUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Asset:
    asset = await asset_for_tenant(session, asset_id, user)
    require_asset_editor(asset, user)
    if asset.scope == AssetScope.PROJECT and asset.project_id:
        await project_for_user(session, asset.project_id, user)
    values = payload.model_dump(exclude_unset=True)
    if "parent_asset_id" in values:
        parent_id = values.pop("parent_asset_id")
        if parent_id and await session.scalar(
            select(Asset.id).where(Asset.parent_asset_id == asset.id).limit(1)
        ):
            raise HTTPException(status_code=409, detail="含有衍生资产的基础资产不能改为衍生资产")
        await validate_parent(
            session,
            parent_id,
            scope=asset.scope,
            project_id=asset.project_id,
            tenant_id=user.tenant_id,
            asset_type=asset.asset_type,
            child_id=asset.id,
        )
        asset.parent_asset_id = parent_id
    for field, value in values.items():
        setattr(asset, field, value)
    if payload.generation_prompt is not None:
        asset.status = (
            AssetStatus.READY
            if asset.media_url
            else AssetStatus.PROMPT_READY
            if payload.generation_prompt.strip()
            else AssetStatus.EXTRACTED
        )
    if payload.media_url is not None:
        asset.status = AssetStatus.READY if payload.media_url else AssetStatus.PROMPT_READY
    asset.version += 1
    await snapshot_asset_revision(session, asset, change_type="manual_update")
    await session.commit()
    await session.refresh(asset)
    return asset


@router.post("/assets/{asset_id}/image/upload", response_model=AssetPublic)
async def upload_asset_image(
    asset_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Asset:
    asset = await asset_for_tenant(session, asset_id, user)
    require_asset_editor(asset, user)
    if asset.asset_type == AssetType.AUDIO:
        raise HTTPException(status_code=409, detail="音频资产不能上传图片")
    if asset.scope == AssetScope.PROJECT and asset.project_id:
        await project_for_user(session, asset.project_id, user)
    if file.content_type not in ALLOWED_COVER_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 JPG、PNG 或 WebP 图片")

    data = await file.read(MAX_COVER_BYTES + 1)
    await file.close()
    if len(data) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="资产图片不能超过 8 MB")
    if not data:
        raise HTTPException(status_code=422, detail="上传的图片为空")

    try:
        _local_url, stored_path = await run_in_threadpool(
            save_asset_image,
            data,
            uploads_root=get_settings().uploads_root,
            tenant_id=user.tenant_id,
            project_id=asset.project_id,
            asset_id=asset.id,
        )
    except InvalidCoverImage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        storage_key, media_url = await persist_media_file(stored_path, "image/webp")
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise

    asset.media_url = media_url
    asset.status = AssetStatus.READY
    asset.asset_metadata = {
        **(asset.asset_metadata or {}),
        "image_source": "user_upload",
        "original_filename": file.filename or "asset-image",
    }
    asset.version += 1
    await snapshot_asset_revision(session, asset, change_type="image_upload")
    try:
        await session.commit()
    except Exception:
        await delete_media_file(storage_key, stored_path)
        await session.rollback()
        raise
    await session.refresh(asset)
    return asset


@router.get("/assets/{asset_id}/revisions", response_model=list[AssetRevisionPublic])
async def list_asset_revisions(
    asset_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AssetRevision]:
    asset = await asset_for_tenant(session, asset_id, user)
    if asset.scope == AssetScope.PROJECT and asset.project_id:
        await project_for_user(session, asset.project_id, user)
    return list(
        (
            await session.scalars(
                select(AssetRevision)
                .where(
                    AssetRevision.asset_id == asset.id,
                    AssetRevision.tenant_id == user.tenant_id,
                )
                .order_by(AssetRevision.version.desc())
            )
        ).all()
    )


@router.post(
    "/assets/{asset_id}/revisions/{revision_id}/restore",
    response_model=AssetPublic,
)
async def restore_asset_revision(
    asset_id: str,
    revision_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Asset:
    asset = await asset_for_tenant(session, asset_id, user)
    require_asset_editor(asset, user)
    if asset.scope == AssetScope.PROJECT and asset.project_id:
        await project_for_user(session, asset.project_id, user)
    revision = await session.get(AssetRevision, revision_id)
    if revision is None or revision.asset_id != asset.id or revision.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="资产历史版本不存在")
    if revision.parent_asset_id and await session.scalar(
        select(Asset.id).where(Asset.parent_asset_id == asset.id).limit(1)
    ):
        raise HTTPException(status_code=409, detail="含有衍生资产的基础资产不能恢复为衍生资产")
    await validate_parent(
        session,
        revision.parent_asset_id,
        scope=asset.scope,
        project_id=asset.project_id,
        tenant_id=user.tenant_id,
        asset_type=asset.asset_type,
        child_id=asset.id,
    )
    asset.parent_asset_id = revision.parent_asset_id
    asset.name = revision.name
    asset.description = revision.description
    asset.generation_prompt = revision.generation_prompt
    asset.media_url = revision.media_url
    asset.status = revision.status
    asset.asset_metadata = dict(revision.asset_metadata or {})
    asset.version += 1
    await snapshot_asset_revision(
        session,
        asset,
        change_type="restore",
        source_revision_id=revision.id,
    )
    await session.commit()
    await session.refresh(asset)
    return asset


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    asset = await asset_for_tenant(session, asset_id, user)
    require_asset_editor(asset, user)
    if asset.scope == AssetScope.PROJECT and asset.project_id:
        await project_for_user(session, asset.project_id, user)
    if await session.scalar(select(Asset.id).where(Asset.parent_asset_id == asset.id).limit(1)):
        raise HTTPException(status_code=409, detail="请先删除或调整该基础资产下的衍生资产")
    await session.delete(asset)
    await session.commit()


@router.post("/projects/{project_id}/assets/{asset_id}/export-global", response_model=AssetPublic)
async def export_asset_to_global(
    project_id: str,
    asset_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Asset:
    await project_for_user(session, project_id, user)
    source = await asset_for_tenant(session, asset_id, user)
    if source.scope != AssetScope.PROJECT or source.project_id != project_id:
        raise HTTPException(status_code=422, detail="只能导出当前项目的塑造资产")
    copied = await copy_asset_between_libraries(
        session,
        source,
        user=user,
        scope=AssetScope.GLOBAL,
        project_id=None,
    )
    await session.commit()
    await session.refresh(copied)
    return copied


@router.post("/projects/{project_id}/assets/import/{asset_id}", response_model=AssetPublic)
async def import_global_asset(
    project_id: str,
    asset_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Asset:
    await project_for_user(session, project_id, user)
    source = await asset_for_tenant(session, asset_id, user)
    if source.scope != AssetScope.GLOBAL:
        raise HTTPException(status_code=422, detail="只能从全局资产库导入")
    copied = await copy_asset_between_libraries(
        session,
        source,
        user=user,
        scope=AssetScope.PROJECT,
        project_id=project_id,
    )
    await session.commit()
    await session.refresh(copied)
    return copied


@router.post(
    "/projects/{project_id}/chapters/{chapter_id}/asset-extractions",
    response_model=AssetExtractionResult,
    status_code=status.HTTP_201_CREATED,
)
async def save_asset_extraction(
    project_id: str,
    chapter_id: str,
    payload: AssetExtractionCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AssetExtractionResult:
    await project_for_user(session, project_id, user)
    chapter = await session.get(Chapter, chapter_id)
    if chapter is None or chapter.project_id != project_id or chapter.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="章节不存在")
    if not chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="提取资产前必须先选择生效剧本")
    script = await session.get(ScriptVersion, chapter.active_script_version_id)
    if script is None or script.chapter_id != chapter.id:
        raise HTTPException(status_code=409, detail="章节生效剧本不可用")
    latest = await session.scalar(
        select(func.max(AssetExtraction.version)).where(AssetExtraction.chapter_id == chapter.id)
    )
    await session.execute(
        update(AssetExtraction)
        .where(AssetExtraction.chapter_id == chapter.id, AssetExtraction.is_active.is_(True))
        .values(is_active=False, invalidated_reason="已生成新的资产提取版本")
    )
    extraction = AssetExtraction(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        chapter_id=chapter.id,
        script_version_id=script.id,
        version=(latest or 0) + 1,
        is_active=True,
    )
    session.add(extraction)
    await session.flush()
    assets: list[Asset] = []
    for item in payload.assets:
        if item.parent_asset_id:
            await validate_parent(
                session,
                item.parent_asset_id,
                scope=AssetScope.PROJECT,
                project_id=project_id,
                tenant_id=user.tenant_id,
                asset_type=item.asset_type,
            )
        asset = new_asset(item, user=user, scope=AssetScope.PROJECT, project_id=project_id)
        session.add(asset)
        await session.flush()
        await snapshot_asset_revision(session, asset, change_type="manual_extraction")
        session.add(AssetExtractionItem(extraction_id=extraction.id, asset_id=asset.id))
        assets.append(asset)
    chapter.status = ChapterStatus.ASSETS
    await session.commit()
    await session.refresh(extraction)
    for asset in assets:
        await session.refresh(asset)
    return AssetExtractionResult(
        extraction=AssetExtractionPublic.model_validate(extraction),
        assets=[AssetPublic.model_validate(asset) for asset in assets],
    )


@router.get(
    "/projects/{project_id}/chapters/{chapter_id}/asset-extractions",
    response_model=list[AssetExtractionPublic],
)
async def list_asset_extractions(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AssetExtraction]:
    await project_for_user(session, project_id, user)
    chapter = await session.get(Chapter, chapter_id)
    if chapter is None or chapter.project_id != project_id or chapter.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="章节不存在")
    return list(
        (
            await session.scalars(
                select(AssetExtraction)
                .where(AssetExtraction.chapter_id == chapter.id)
                .order_by(AssetExtraction.version.desc())
            )
        ).all()
    )
