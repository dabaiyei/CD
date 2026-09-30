from __future__ import annotations

import hashlib
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.tasks import (
    latest_events_for_tasks,
    task_for_user,
    task_public,
    task_public_with_latest_event,
)
from app.core.config import get_settings
from app.db.models import (
    AIModel,
    AITask,
    Asset,
    AssetType,
    ModelType,
    PersonalAgentAttachment,
    Provider,
    TaskStatus,
    User,
)
from app.db.session import get_session
from app.services import hypit_bridge
from app.services.object_storage import persist_media_file
from app.services.task_events import publish_task_event, record_task_event
from app.services.task_queue import enqueue_task
from app.services.video_replica import (
    ANALYZE,
    EDIT,
    EXPORT,
    IMAGE,
    MAX_BYTES,
    RENDER,
    SPEECH,
    ImageInput,
    RenderInput,
    model_for,
    validate_coverage,
)

router = APIRouter(prefix="/video-replicas", tags=["video-replicas"])


@router.get("/config")
async def config(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    models = (
        await db.scalars(
            select(AIModel)
            .join(Provider)
            .where(
                AIModel.tenant_id == user.tenant_id,
                Provider.tenant_id == user.tenant_id,
                AIModel.enabled.is_(True),
                Provider.enabled.is_(True),
                AIModel.model_type.in_([ModelType.TEXT, ModelType.VIDEO, ModelType.IMAGE, ModelType.TTS]),
            )
        )
    ).all()
    return {
        "available": hypit_bridge.available(),
        "replica_pipeline_version": 3,
        "max_bytes": MAX_BYTES,
        "max_seconds": 180,
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "type": m.model_type.value,
                "is_default": m.is_default,
                "capabilities": m.capabilities,
            }
            for m in models
        ],
    }


@router.get("")
async def listing(
    limit: int = Query(60, ge=1, le=300),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    tasks = (
        await db.scalars(
            select(AITask)
            .where(
                AITask.tenant_id == user.tenant_id,
                AITask.user_id == user.id,
                AITask.task_type.in_([ANALYZE, RENDER, IMAGE, EXPORT, EDIT, SPEECH]),
            )
            .order_by(AITask.created_at.desc())
            .limit(limit)
        )
    ).all()
    events = await latest_events_for_tasks(db, [t.id for t in tasks])
    return [task_public(t, events.get(t.id)) for t in tasks]


class ReplicaMetadata(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=120)
    archived: bool | None = None
    editor_draft: RenderInput | None = None


@router.patch("/{task_id}")
async def update_metadata(
    task_id: str,
    body: ReplicaMetadata,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    task = await task_for_user(db, task_id, user, for_update=True)
    if task.task_type not in (ANALYZE, RENDER, IMAGE, EXPORT, EDIT, SPEECH):
        raise HTTPException(404, "复刻任务不存在")
    if body.archived and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        raise HTTPException(409, "请等待任务结束后归档")
    payload = dict(task.request_payload or {})
    if body.display_name is not None:
        if not body.display_name.strip():
            raise HTTPException(422, "任务名称不能为空")
        payload["display_name"] = body.display_name.strip()
    if body.archived is not None:
        payload["archived"] = body.archived
    if body.editor_draft is not None:
        if task.task_type != ANALYZE or task.status != TaskStatus.SUCCEEDED:
            raise HTTPException(409, "只有已完成的参考分析可保存编辑草稿")
        payload["editor_reference_snapshots"] = await resolve_references(
            db, user, body.editor_draft.references
        )
        payload["editor_draft"] = body.editor_draft.model_dump()
    task.request_payload = payload
    await db.commit()
    return await task_public_with_latest_event(db, task)


@router.post("", status_code=202)
async def create(
    brief: str = Form(..., min_length=1, max_length=6000),
    text_model_id: str = Form(...),
    transcript: str = Form("", max_length=10000),
    auto_transcribe: bool = Form(True),
    transcription_language: Literal["zh", "en"] = Form("zh"),
    source_url: str = Form("", max_length=2000),
    file: UploadFile | None = File(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    if not hypit_bridge.available():
        raise HTTPException(503, "本地 Hypit 尚未安装")
    if bool(file) == bool(source_url.strip()):
        raise HTTPException(422, "请上传一个视频或填写一个视频直链")
    try:
        await model_for(db, user.tenant_id, text_model_id, ModelType.TEXT)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    task_id = str(uuid4())
    payload = {"brief": brief.strip(), "transcript": transcript.strip(), "text_model_id": text_model_id}
    payload.update(
        auto_transcribe=auto_transcribe, transcription_language=transcription_language, analysis_version=3
    )
    if file:
        root = get_settings().uploads_root / user.tenant_id / user.id / "video-replicas" / task_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / "source.mp4"
        try:
            size = 0
            with path.open("wb") as output:
                while data := await file.read(1024 * 1024):
                    size += len(data)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "参考视频不能超过 100MB")
                    output.write(data)
            if not size:
                raise HTTPException(422, "视频不能为空")
            key, url = await persist_media_file(path, file.content_type or "video/mp4")
            payload.update({"source_key": key, "source_url": url, "filename": file.filename or "参考视频"})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
    else:
        from app.services.media_gateway import _validate_download_url

        try:
            await _validate_download_url(source_url.strip(), media_name="参考视频")
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc
        payload.update({"remote_url": source_url.strip(), "filename": "链接参考视频"})
    task = AITask(
        id=task_id,
        tenant_id=user.tenant_id,
        user_id=user.id,
        task_type=ANALYZE,
        model_id=text_model_id,
        cost=0,
        request_payload=payload,
    )
    db.add(task)
    await db.flush()
    event = record_task_event(db, task, status=TaskStatus.QUEUED, progress=0, message="参考视频分析已排队")
    await db.commit()
    await publish_task_event(task, event)
    await enqueue_task(task.id)
    return await task_public_with_latest_event(db, task)


async def prepare_render(task_id, options, user, db):
    from app.services.replica_planning import generation_plan

    original = await task_for_user(db, task_id, user)
    if original.task_type != ANALYZE or original.status != TaskStatus.SUCCEEDED:
        raise HTTPException(409, "请先完成参考视频分析")
    media = (original.result_payload or {}).get("media", {})
    references = await resolve_references(db, user, options.references)
    try:
        validate_coverage(options.plan.shots, 0, float(media["duration"]))
        model, _ = await model_for(db, user.tenant_id, options.video_model_id, ModelType.VIDEO)
        if options.aspect_ratio not in (model.capabilities or {}).get(
            "aspect_ratios", [options.aspect_ratio]
        ):
            raise ValueError("当前模型不支持该画幅比例")
        layout = generation_plan(options, model.capabilities or {}, references)
        text_id = options.text_model_id or original.request_payload.get("text_model_id")
        text_model, _ = await model_for(db, user.tenant_id, text_id, ModelType.TEXT)
        if references and (text_model.capabilities or {}).get("supports_vision") is False:
            raise ValueError("人物改编需要支持图片理解的文本模型")
        if any(u["prepare_frame"] for u in layout["units"]):
            image_model, _ = await model_for(db, user.tenant_id, options.image_model_id, ModelType.IMAGE)
            caps = image_model.capabilities or {}
            if "image_to_image" not in caps.get("generation_modes", ["image_to_image"]):
                raise ValueError("场景参考图模型必须支持图生图")
            if caps.get("aspect_ratios") and options.aspect_ratio not in caps["aspect_ratios"]:
                raise ValueError("场景参考图模型不支持当前画幅")
            limit = (caps.get("reference_limits") or {}).get("image") or {}
            for unit in layout["units"]:
                if unit["prepare_frame"] and (
                    limit.get("enabled") is False
                    or len(unit["reference_indices"]) + 1 > (limit.get("max_count") or 8)
                ):
                    raise ValueError("场景参考图模型无法容纳人物图片和原片构图，请选择支持多图的图片模型")
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return original, media, references, layout


@router.post("/{task_id}/render-plan")
async def preview_render(
    task_id: str,
    options: RenderInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _, _, _, layout = await prepare_render(task_id, options, user, db)
    return layout


@router.post("/{task_id}/render", status_code=202)
async def render(
    task_id: str,
    options: RenderInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    original, media, references, layout = await prepare_render(task_id, options, user, db)
    import json

    identity = (
        "replica-v2-"
        + hashlib.sha256(
            (original.id + options.model_dump_json() + json.dumps(references, sort_keys=True)).encode()
        ).hexdigest()
    )
    existing = await db.scalar(
        select(AITask).where(
            AITask.idempotency_key == identity, AITask.tenant_id == user.tenant_id, AITask.user_id == user.id
        )
    )
    if existing:
        return await task_public_with_latest_event(db, existing)
    task = AITask(
        tenant_id=user.tenant_id,
        user_id=user.id,
        task_type=RENDER,
        idempotency_key=identity,
        model_id=options.video_model_id,
        cost=0,
        request_payload={
            "analysis_id": original.id,
            "pipeline_version": 2,
            "generation_plan": layout,
            "text_model_id": options.text_model_id or original.request_payload["text_model_id"],
            "source_key": original.request_payload.get("source_key")
            or (original.result_payload or {}).get("source_key"),
            "filename": original.request_payload.get("filename"),
            "has_audio": bool(media.get("hasAudio")),
            "options": options.model_dump(),
            "brief": original.request_payload["brief"],
            "reference_snapshots": references,
            "production_documents": (original.result_payload or {}).get("production_documents", {}),
            "transcription": (original.result_payload or {}).get("transcription"),
        },
    )
    try:
        db.add(task)
        await db.flush()
        event = record_task_event(db, task, status=TaskStatus.QUEUED, progress=0, message="视频复刻已排队")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(AITask).where(AITask.idempotency_key == identity))
        if existing is None:
            raise
        return await task_public_with_latest_event(db, existing)
    await publish_task_event(task, event)
    await enqueue_task(task.id)
    return await task_public_with_latest_event(db, task)


async def resolve_references(db, user, references):
    from app.services.object_storage import object_key_from_media_url

    resolved = []
    for reference in references:
        if reference.kind == "asset":
            from app.api.routes.assets import asset_for_user

            item = await asset_for_user(db, reference.id, user)
            if item.asset_type == AssetType.AUDIO:
                raise HTTPException(422, "请选择图片资产")
            url, name = item.media_url, item.name
        elif reference.kind == "attachment":
            item = await db.get(PersonalAgentAttachment, reference.id)
            if not item or item.user_id != user.id or item.tenant_id != user.tenant_id:
                raise HTTPException(404, "参考图片不存在")
            if not item.mime_type.startswith("image/"):
                raise HTTPException(422, "参考附件必须是图片")
            url, name = item.media_url, item.name
        else:
            task = await task_for_user(db, reference.id, user)
            if task.task_type != IMAGE or task.status != TaskStatus.SUCCEEDED:
                raise HTTPException(422, "参考图尚未生成完成")
            url, name = (task.result_payload or {}).get("media_url"), "AI 参考图"
        key = object_key_from_media_url(url)
        if not key:
            raise HTTPException(422, "参考图文件不可读取，请重新上传")
        resolved.append({**reference.model_dump(), "url": url, "name": name, "storage_key": key})
    return resolved


@router.get("/assets")
async def reference_assets(
    search: str = Query("", max_length=160),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    rows = (
        await db.scalars(
            select(Asset)
            .where(
                Asset.tenant_id == user.tenant_id,
                Asset.user_id == user.id,
                Asset.asset_type != AssetType.AUDIO,
                Asset.media_url.is_not(None),
                Asset.name.contains(search, autoescape=True),
            )
            .order_by(Asset.updated_at.desc(), Asset.id)
            .offset(offset)
            .limit(60)
        )
    ).all()
    return [{"id": a.id, "name": a.name, "url": a.media_url, "scope": a.scope.value} for a in rows]


@router.post("/{task_id}/images", status_code=202)
async def create_reference_image(
    task_id: str,
    options: ImageInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    original = await task_for_user(db, task_id, user)
    if original.task_type != ANALYZE:
        raise HTTPException(422, "请选择参考分析任务")
    try:
        model, _ = await model_for(db, user.tenant_id, options.model_id, ModelType.IMAGE)
        caps = model.capabilities or {}
        for field, value in [("resolutions", options.resolution), ("aspect_ratios", options.aspect_ratio)]:
            if caps.get(field) and value not in caps[field]:
                raise ValueError("图片参数不在当前模型支持范围内")
        if (
            options.references
            and caps.get("generation_modes")
            and "image_to_image" not in caps["generation_modes"]
        ):
            raise ValueError("当前图片模型不支持图生图")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    references = await resolve_references(db, user, options.references)
    task = AITask(
        tenant_id=user.tenant_id,
        user_id=user.id,
        task_type=IMAGE,
        model_id=model.id,
        cost=0,
        request_payload={
            "analysis_id": original.id,
            "filename": "AI 参考图",
            "options": options.model_dump(),
            "reference_snapshots": references,
        },
    )
    db.add(task)
    await db.flush()
    event = record_task_event(db, task, status=TaskStatus.QUEUED, progress=0, message="复刻参考图生成已排队")
    await db.commit()
    await publish_task_event(task, event)
    await enqueue_task(task.id)
    return await task_public_with_latest_event(db, task)
