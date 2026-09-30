"""Account scoped persistence and queued generation for the bundled canvas."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import AIModel, CanvasStorageItem, ModelType, Provider, User, UserRole
from app.db.session import get_session
from app.services.billing import resolve_task_pricing
from app.services.object_storage import object_storage
from app.services.provider_adapters import VideoModelCapabilities
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import create_queued_task

router = APIRouter(prefix="/canvas", tags=["infinite-canvas"])
MAX_BYTES = 100 * 1024 * 1024


def scope(user, namespace, key=None):
    if len(namespace) > 100 or not namespace or (key is not None and (not key or len(key) > 250)):
        raise HTTPException(422, "画布存储键不合法")
    clauses = [
        CanvasStorageItem.tenant_id == user.tenant_id,
        CanvasStorageItem.user_id == user.id,
        CanvasStorageItem.namespace == namespace,
    ]
    if key is not None:
        clauses.append(CanvasStorageItem.key == key)
    return clauses


@router.get("/config")
async def config(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = (
        await db.scalars(
            select(AIModel)
            .join(Provider)
            .where(
                AIModel.tenant_id == user.tenant_id,
                Provider.tenant_id == user.tenant_id,
                AIModel.enabled.is_(True),
                Provider.enabled.is_(True),
            )
        )
    ).all()
    return {
        "user_id": user.id,
        "is_admin": user.role == UserRole.ADMIN,
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "type": m.model_type.value,
                "is_default": m.is_default,
                "capabilities": (
                    VideoModelCapabilities.model_validate(m.capabilities or {}).model_dump()
                    if m.model_type == ModelType.VIDEO
                    else m.capabilities
                ),
            }
            for m in rows
        ],
    }


@router.get("/storage/{namespace}")
async def keys(
    namespace: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    return list(
        (
            await db.scalars(
                select(CanvasStorageItem.key).where(*scope(user, namespace)).order_by(CanvasStorageItem.key)
            )
        ).all()
    )


@router.get("/storage/{namespace}/{key}")
async def read(
    namespace: str, key: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    item = await db.scalar(select(CanvasStorageItem).where(*scope(user, namespace, key)))
    if item is None:
        return Response(status_code=204, headers={"X-Canvas-Revision": "0"})
    data = await object_storage().get_bytes(item.storage_path) if item.storage_path else item.value or "null"
    return Response(
        content=data,
        media_type=item.mime_type,
        headers={
            "X-Canvas-Revision": str(item.revision),
            "X-Canvas-Kind": item.kind,
            "Cache-Control": "no-store",
        },
    )


@router.put("/storage/{namespace}/{key}")
async def write(
    namespace: str,
    key: str,
    request: Request,
    revision: int = Query(ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    clauses = scope(user, namespace, key)
    kind = request.headers.get("X-Canvas-Kind", "json")
    mime = request.headers.get("Content-Type", "application/json").split(";")[0]
    if kind not in {"json", "blob"} or (
        kind == "blob" and not mime.startswith(("image/", "video/", "audio/"))
    ):
        raise HTTPException(422, "仅支持画布数据、图片、视频与音频")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "单个画布文件不能超过100MB")
    value, storage_path = None, None
    if kind == "json":
        try:
            value = bytes(data).decode("utf-8")
            json.loads(value)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, "画布数据不是有效JSON") from exc
        mime = "application/json"
    elif mime.startswith("image/"):
        from app.services.media import InvalidCoverImage, validate_uploaded_image

        try:
            await asyncio.to_thread(validate_uploaded_image, bytes(data))
        except InvalidCoverImage as exc:
            raise HTTPException(422, str(exc)) from exc
    item = await db.scalar(select(CanvasStorageItem).where(*clauses))
    if (item.revision if item else 0) != revision:
        raise HTTPException(409, "画布已在其它窗口更新，请刷新后继续，当前修改未覆盖")
    if kind == "blob":
        suffix = mimetypes.guess_extension(mime) or ".bin"
        storage_path = f"{user.tenant_id}/canvas/{user.id}/{uuid4()}{suffix}"
        await object_storage().put_bytes(storage_path, bytes(data), mime)
    previous_path = item.storage_path if item else None
    try:
        values = dict(
            kind=kind, value=value, storage_path=storage_path, mime_type=mime, revision=revision + 1
        )
        if item:
            result = await db.execute(
                update(CanvasStorageItem)
                .where(*clauses, CanvasStorageItem.revision == revision)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise HTTPException(409, "画布已在其它窗口更新，请刷新后继续")
        else:
            db.add(
                CanvasStorageItem(
                    tenant_id=user.tenant_id, user_id=user.id, namespace=namespace, key=key, **values
                )
            )
        await db.commit()
    except (IntegrityError, HTTPException) as exc:
        await db.rollback()
        if storage_path:
            await object_storage().delete(storage_path)
        raise HTTPException(409, "画布已在其它窗口更新，请刷新后继续") from exc
    if previous_path:
        await object_storage().delete(previous_path)
    return {"revision": revision + 1}


@router.delete("/storage/{namespace}/{key}", status_code=204)
async def remove(
    namespace: str, key: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    item = await db.scalar(select(CanvasStorageItem).where(*scope(user, namespace, key)))
    if item:
        path = item.storage_path
        await db.delete(item)
        await db.commit()
        if path:
            await object_storage().delete(path)


class GenerationInput(BaseModel):
    model_id: str
    expected_kind: str | None = Field(default=None, pattern="^(text|image|video|tts)$")
    prompt: str = Field(min_length=1, max_length=32000)
    reference_keys: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    aspect_ratio: str = "16:9"
    resolution: str = "1K"
    duration_seconds: float = Field(default=6, gt=0, le=120)
    audio_enabled: bool = True
    video_mode: str = Field(default="reference", pattern="^(reference|frames)$")
    voice: str = "alloy"
    instructions: str = Field(default="", max_length=4000)


@router.post("/generate", status_code=202)
async def generate(
    payload: GenerationInput, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    from app.services.infinite_canvas import compress_reference, reference_mime, validate_generation

    model = await db.scalar(
        select(AIModel)
        .join(Provider)
        .where(
            AIModel.id == payload.model_id,
            AIModel.tenant_id == user.tenant_id,
            Provider.tenant_id == user.tenant_id,
            AIModel.enabled.is_(True),
            Provider.enabled.is_(True),
        )
    )
    if not model:
        raise HTTPException(422, "所选模型不可用，请在管理页配置")
    if payload.expected_kind and payload.expected_kind != model.model_type.value:
        raise HTTPException(422, "节点类型与所选模型不一致，请选择对应的生成模型")
    references = []
    for ref in payload.reference_keys:
        row = await db.scalar(
            select(CanvasStorageItem).where(*scope(user, ref.get("namespace", ""), ref.get("key", "")))
        )
        if (
            row is None
            or not row.storage_path
            or not row.mime_type.startswith(("image/", "video/", "audio/"))
        ):
            raise HTTPException(422, "参考素材不存在或不属于当前账号")
        references.append(
            {
                "storage_path": row.storage_path,
                "mime_type": reference_mime(row.mime_type, model),
                "purpose": ref.get("purpose", "参考素材"),
            }
        )
    try:
        validate_generation(model, payload, references)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    billing_type = {
        ModelType.IMAGE: "asset_image_generation",
        ModelType.VIDEO: "shot_video_generation",
        ModelType.TEXT: "agent_chat_run",
        ModelType.TTS: "dialogue_tts_generation",
    }[model.model_type]
    pricing = await resolve_task_pricing(db, tenant_id=user.tenant_id, task_type=billing_type)
    # Tasks retain immutable inputs even if an upstream node is edited or deleted.
    snapshots = []
    try:
        reference_batch = str(uuid4())
        for index, ref in enumerate(references):
            data = await object_storage().get_bytes(ref["storage_path"])
            data = await asyncio.to_thread(compress_reference, data, ref["mime_type"])
            key = f"{user.tenant_id}/canvas/{user.id}/task-inputs/{reference_batch}/{index}"
            await object_storage().put_bytes(key, data, ref["mime_type"])
            snapshots.append(key)
            ref["storage_path"] = key
    except Exception:
        for key in snapshots:
            await object_storage().delete(key)
        raise
    try:
        task, event = await create_queued_task(
            db,
            user=user,
            project_id=None,
            task_type="canvas_generation",
            model_id=model.id,
            cost=pricing.total_cost,
            request_payload={
                **payload.model_dump(),
                "kind": model.model_type.value,
                "references": references,
                "pricing": pricing.as_payload(),
            },
            message="无限画布生成",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        for key in snapshots:
            await object_storage().delete(key)
        raise
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return {"id": task.id}
