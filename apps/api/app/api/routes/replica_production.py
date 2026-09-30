"""Authenticated native production editing; no caller-selected paths or commands."""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.api.deps import get_current_user
from app.api.routes.tasks import task_for_user, task_public_with_latest_event
from app.db.models import AITask, ModelType, TaskStatus, User
from app.db.session import get_session
from app.services import hypit_bridge, replica_cut
from app.services import replica_production as production
from app.services.object_storage import persist_media_file
from app.services.task_events import publish_task_event, record_task_event
from app.services.task_queue import enqueue_task
from app.services.video_replica import EDIT, EXPORT, MAX_BYTES, RENDER, SPEECH, model_for

router = APIRouter(prefix="/video-replicas", tags=["video-replicas"])


async def owned(db, task_id, user):
    task = await task_for_user(db, task_id, user)
    if task.task_type not in (RENDER, EDIT, EXPORT, SPEECH):
        raise HTTPException(404, "制作工程不存在")
    value = (task.request_payload or {}).get("production_draft") or (task.result_payload or {}).get(
        "production"
    )
    if not value and task.task_type == RENDER and task.status == TaskStatus.SUCCEEDED:
        from app.services.object_storage import materialize_media_file
        from app.services.video_replica import output_size

        previous = (task.result_payload or {}).get("clips", {})
        if previous and all(item.get("key") for item in previous.values()):
            clips = [
                {**previous[key], "path": str(await materialize_media_file(previous[key]["key"]))}
                for key in sorted(previous, key=int)
            ]
            options = task.request_payload.get("options", {})
            width, height = output_size(
                options.get("resolution", "720p"), options.get("aspect_ratio", "16:9")
            )
            with tempfile.TemporaryDirectory(prefix="hypit-migrate-") as directory:
                root = Path(directory)
                hypit_bridge.write_composition(root, clips, width=width, height=height)
                value = production.capture(root, clips, task.request_payload.get("production_documents", {}))
            task.result_payload = {**(task.result_payload or {}), "production": value}
            await db.commit()
    if not value:
        raise HTTPException(409, "请先完成视频素材生成，再打开制作工程")
    return task, json.loads(json.dumps(value))


async def store(db, task, value, expected):
    previous = dict(task.request_payload or {})
    current = previous.get("production_draft") or (task.result_payload or {}).get("production", {})
    if current.get("revision") != expected:
        raise HTTPException(409, "制作工程已被其他窗口修改，请重新加载后再保存")
    value["revision"] = hashlib.sha256(
        json.dumps(
            {"files": value["files"], "assets": value["assets"]},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    result = await db.execute(
        update(AITask)
        .where(
            AITask.id == task.id,
            AITask.request_payload == previous,
        )
        .values(request_payload={**previous, "production_draft": value})
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "工程已更新，请重新加载后再保存")
    await db.commit()
    return value


@router.get("/production/vocabulary")
async def vocabulary(
    components: str = Query("media-track", max_length=200), user: User = Depends(get_current_user)
):
    try:
        return await production.vocabulary(components.split(","))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{task_id}/production")
async def get_production(
    task_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    _, value = await owned(db, task_id, user)
    return value


class SaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    files: dict[str, str]


@router.get("/{task_id}/production/cut")
async def get_cut(
    task_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    _, value = await owned(db, task_id, user)
    return replica_cut.view(value)


@router.put("/{task_id}/production/cut")
async def save_cut(
    task_id: str,
    body: replica_cut.CutInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    task, value = await owned(db, task_id, user)
    if value["revision"] != body.revision:
        raise HTTPException(409, "工程已更新，请重新加载剪辑")
    try:
        candidate = replica_cut.apply(value, body.clips, close_gaps=body.close_gaps)
        with tempfile.TemporaryDirectory(prefix="hypit-cut-") as folder:
            await production.check(candidate, Path(folder))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    saved = await store(db, task, candidate, body.revision)
    return replica_cut.view(saved)


@router.put("/{task_id}/production")
async def save_production(
    task_id: str,
    body: SaveInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    task, value = await owned(db, task_id, user)
    value["files"] = body.files
    try:
        production.validate_sources(value["files"], value["assets"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Native compilation is a separate explicit check; drafts may be work in progress.
    return await store(db, task, value, body.revision)


@router.post("/{task_id}/production/check")
async def check_production(
    task_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    _, value = await owned(db, task_id, user)
    try:
        with tempfile.TemporaryDirectory(prefix="hypit-check-") as folder:
            report = await production.check(value, Path(folder))
        return {"valid": True, "revision": value["revision"], "report": report}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


class CreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    instruction: str = Field(default="", max_length=4000)
    text_model_id: str | None = None
    language: Literal["zh", "en"] = "zh"
    speech: production.SpeechInput | None = None
    components: list[str] = Field(default_factory=lambda: ["media-track"], min_length=1, max_length=4)


@router.post("/{task_id}/production/{operation}", status_code=202)
async def create_production_task(
    task_id: str,
    operation: str,
    body: CreateInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    if operation not in {"edit", "export", "speech"}:
        raise HTTPException(404, "制作操作不存在")
    parent, value = await owned(db, task_id, user)
    if body.revision != value["revision"]:
        raise HTTPException(409, "工程已更新，请先重新加载")
    try:
        production.validate_sources(value["files"], value["assets"])
        if operation == "edit":
            if not body.instruction.strip():
                raise ValueError("请输入本次修改要求")
            if any(c not in production.PACKAGES for c in body.components):
                raise ValueError("所选制作组件未接入")
            await model_for(db, user.tenant_id, body.text_model_id, ModelType.TEXT)
        if operation == "speech":
            if not body.speech or not body.speech.text.strip() or not body.speech.voice.strip():
                raise ValueError("请选择语音模型、音色并填写配音文本")
            model, _ = await model_for(db, user.tenant_id, body.speech.model_id, ModelType.TTS)
            voices = (model.capabilities or {}).get("voices") or []
            available = {v.get("id", v.get("value")) if isinstance(v, dict) else v for v in voices}
            if available and body.speech.voice not in available:
                raise ValueError("所选音色不在当前配音模型的音色列表中")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    identity = (
        "hypit-native-" + hashlib.sha256((task_id + operation + body.model_dump_json()).encode()).hexdigest()
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
        cost=0,
        task_type=EDIT if operation == "edit" else SPEECH if operation == "speech" else EXPORT,
        idempotency_key=identity,
        model_id=body.text_model_id
        if operation == "edit"
        else body.speech.model_id
        if operation == "speech"
        else None,
        request_payload={
            "production": value,
            "parent_id": parent.id,
            "analysis_id": parent.request_payload.get("analysis_id"),
            "filename": "工程修改"
            if operation == "edit"
            else "AI 配音"
            if operation == "speech"
            else "工程导出",
            "plan": (parent.result_payload or {}).get("plan", parent.request_payload.get("plan", {})),
            "instruction": body.instruction,
            "text_model_id": body.text_model_id,
            "components": body.components,
            "language": body.language,
            "speech": body.speech.model_dump() if body.speech else None,
        },
    )
    db.add(task)
    await db.flush()
    event = record_task_event(
        db,
        task,
        status=TaskStatus.QUEUED,
        progress=0,
        message={
            "edit": "工程修改已排队",
            "speech": "配音素材生成已排队",
            "export": "复用已有素材，制作工程导出已排队",
        }[operation],
    )
    await db.commit()
    await publish_task_event(task, event)
    await enqueue_task(task.id)
    return await task_public_with_latest_event(db, task)


@router.post("/{task_id}/production/assets/upload")
async def upload_asset(
    task_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    from PIL import Image, UnidentifiedImageError

    from app.core.config import get_settings
    from app.services.video_concat import run_media_command

    task, value = await owned(db, task_id, user)
    expected = value["revision"]
    root = get_settings().uploads_root / user.tenant_id / user.id / "video-replicas" / task_id
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=root, prefix="import-") as folder:
            source = Path(folder) / "upload"
            size = 0
            with source.open("wb") as output:
                while data := await file.read(1024 * 1024):
                    size += len(data)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "制作素材不能超过100MB")
                    output.write(data)
            try:
                with Image.open(source) as image:
                    image.load()
                    target = root / f"material-{uuid4().hex}.webp"
                    image.convert("RGBA" if "A" in image.getbands() else "RGB").save(
                        target, "WEBP", quality=90
                    )
                    dimensions = {"width": image.width, "height": image.height}
                kind, mime, duration = "image", "image/webp", None
            except (UnidentifiedImageError, OSError):
                try:
                    info = json.loads(
                        await run_media_command(
                            "ffprobe",
                            "-v",
                            "error",
                            "-show_streams",
                            "-show_format",
                            "-of",
                            "json",
                            str(source),
                        )
                    )
                    video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
                    audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
                    if not video and not audio:
                        raise ValueError("没有可用的音视频轨道")
                    duration = float(info["format"]["duration"])
                    if not 0 < duration <= 600:
                        raise ValueError("单份音视频素材最长10分钟")
                    target = root / f"material-{uuid4().hex}.{'mp4' if video else 'wav'}"
                    args = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
                    args += (
                        [
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0?",
                            "-c:v",
                            "libx264",
                            "-preset",
                            "fast",
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "aac",
                        ]
                        if video
                        else ["-vn", "-c:a", "pcm_s16le"]
                    )
                    await run_media_command(*args, str(target))
                    kind, mime = ("video", "video/mp4") if video else ("audio", "audio/wav")
                    dimensions = {}
                except (ValueError, RuntimeError, KeyError) as exc:
                    raise HTTPException(422, "请选择有效的图片、视频或音频：" + str(exc)[:300]) from exc
            key, url = await persist_media_file(target, mime)
            value["assets"]["assets/" + target.name] = {
                "key": key,
                "type": kind,
                "name": file.filename,
                "duration": duration,
                "url": url,
                **dimensions,
            }
            return await store(db, task, value, expected)
    finally:
        await file.close()


@router.get("/{task_id}/production/archive")
async def archive(
    task_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
):
    _, value = await owned(db, task_id, user)
    folder = tempfile.TemporaryDirectory(prefix="hypit-export-")
    try:
        root = Path(folder.name)
        project = root / "project"
        await production.materialize(value, project)
        profile_path = project / "hypit.runtime.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if "whisperx.local" in profile["endpoints"]:
            profile["endpoints"]["whisperx.local"]["config"]["modelCacheDirectory"] = ".hypit/speech-models"
            profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        package = root / "hypit-production.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as output:
            for path in project.rglob("*"):
                if path.is_file():
                    output.write(path, str(path.relative_to(project)))
            license_path = hypit_bridge.INSTALL / "node_modules/@hypit/hypit/LICENSE"
            if license_path.exists():
                output.write(license_path, "HYPIT-LICENSE.txt")
            output.writestr(
                "README.txt",
                "Powered by Hypit 0.2.16\nInstall @hypit/hypit@0.2.16.\n"
                "hypit studio --run render.svrun --runtime hypit.runtime.json\n"
                "hypit build render.svrun --runtime hypit.runtime.json --follow\n"
                "This archive contains no API keys.\n",
            )
        return FileResponse(
            package,
            media_type="application/zip",
            filename="hypit-production.zip",
            background=BackgroundTask(folder.cleanup),
        )
    except BaseException:
        folder.cleanup()
        raise
