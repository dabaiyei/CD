from __future__ import annotations

import logging
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_admin
from app.core.config import get_settings
from app.db.models import PlatformBranding, User
from app.db.session import get_session
from app.domain.schemas import LoginBackgroundVideoUrlUpdate, PlatformBrandingPublic
from app.services.object_storage import delete_media_file, persist_media_file

router = APIRouter(tags=["branding"])
logger = logging.getLogger(__name__)

DEFAULT_LOGIN_BACKGROUND_VIDEO_URL = "/videos/login-background.mp4"
MAX_LOGIN_BACKGROUND_VIDEO_BYTES = 300 * 1024 * 1024
ALLOWED_LOGIN_BACKGROUND_VIDEO_TYPES = {"video/mp4", "video/webm"}
UPLOAD_CHUNK_BYTES = 1024 * 1024


def _public_branding(record: PlatformBranding | None) -> PlatformBrandingPublic:
    if (
        record is not None
        and record.login_background_video_source in {"url", "upload"}
        and record.login_background_video_url
    ):
        return PlatformBrandingPublic(
            login_background_video_url=record.login_background_video_url,
            login_background_video_source=record.login_background_video_source,
            updated_at=record.updated_at,
        )
    return PlatformBrandingPublic(
        login_background_video_url=DEFAULT_LOGIN_BACKGROUND_VIDEO_URL,
        login_background_video_source="default",
        updated_at=record.updated_at if record is not None else None,
    )


async def _branding_record(session: AsyncSession) -> PlatformBranding:
    record = await session.get(PlatformBranding, "default")
    if record is None:
        record = PlatformBranding(id="default")
        session.add(record)
        await session.flush()
    return record


def _video_kind(header: bytes) -> tuple[Literal["mp4", "webm"], str]:
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "webm", "video/webm"
    raise ValueError("视频文件内容无效，仅支持标准 MP4 或 WebM 文件")


def _store_video_upload(source: BinaryIO, target_dir: Path) -> tuple[Path, str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    temporary = target_dir / f".{token}.uploading"
    target: Path | None = None
    total = 0
    try:
        first_chunk = source.read(UPLOAD_CHUNK_BYTES)
        if not first_chunk:
            raise ValueError("上传的视频文件为空")
        suffix, content_type = _video_kind(first_chunk)
        target = target_dir / f"login-background-{token}.{suffix}"
        with temporary.open("wb") as output:
            chunk = first_chunk
            while chunk:
                total += len(chunk)
                if total > MAX_LOGIN_BACKGROUND_VIDEO_BYTES:
                    raise OverflowError("登录背景视频不能超过 300 MB")
                output.write(chunk)
                chunk = source.read(UPLOAD_CHUNK_BYTES)
        temporary.replace(target)
        return target, content_type
    except Exception:
        temporary.unlink(missing_ok=True)
        if target is not None:
            target.unlink(missing_ok=True)
        raise


async def _delete_replaced_upload(storage_path: str | None) -> None:
    if not storage_path:
        return
    try:
        await delete_media_file(storage_path, get_settings().uploads_root / storage_path)
    except Exception:
        logger.warning("Failed to delete replaced login background video %s", storage_path, exc_info=True)


@router.get("/public/branding", response_model=PlatformBrandingPublic)
async def get_public_branding(
    session: AsyncSession = Depends(get_session),
) -> PlatformBrandingPublic:
    return _public_branding(await session.get(PlatformBranding, "default"))


@router.get("/admin/branding", response_model=PlatformBrandingPublic)
async def get_admin_branding(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PlatformBrandingPublic:
    return _public_branding(await session.get(PlatformBranding, "default"))


@router.put("/admin/branding/login-background/url", response_model=PlatformBrandingPublic)
async def update_login_background_url(
    payload: LoginBackgroundVideoUrlUpdate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PlatformBrandingPublic:
    record = await _branding_record(session)
    previous_storage_path = record.login_background_video_storage_path
    record.login_background_video_source = "url"
    record.login_background_video_url = payload.url
    record.login_background_video_storage_path = None
    await session.commit()
    await session.refresh(record)
    await _delete_replaced_upload(previous_storage_path)
    return _public_branding(record)


@router.post(
    "/admin/branding/login-background/upload",
    response_model=PlatformBrandingPublic,
    status_code=status.HTTP_201_CREATED,
)
async def upload_login_background_video(
    file: UploadFile = File(...),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PlatformBrandingPublic:
    filename_suffix = Path(file.filename or "").suffix.lower()
    if (
        file.content_type not in ALLOWED_LOGIN_BACKGROUND_VIDEO_TYPES
        and filename_suffix not in {".mp4", ".webm"}
    ):
        await file.close()
        raise HTTPException(status_code=415, detail="仅支持 MP4 或 WebM 视频")

    target: Path | None = None
    storage_path: str | None = None
    try:
        await file.seek(0)
        target, content_type = await run_in_threadpool(
            _store_video_upload,
            file.file,
            get_settings().uploads_root / "system" / "branding",
        )
        storage_path, media_url = await persist_media_file(target, content_type)
    except OverflowError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        if target is not None:
            await run_in_threadpool(target.unlink, missing_ok=True)
        raise
    finally:
        await file.close()

    record = await _branding_record(session)
    previous_storage_path = record.login_background_video_storage_path
    record.login_background_video_source = "upload"
    record.login_background_video_url = media_url
    record.login_background_video_storage_path = storage_path
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        if storage_path:
            await delete_media_file(storage_path, target)
        raise
    await session.refresh(record)
    if previous_storage_path != storage_path:
        await _delete_replaced_upload(previous_storage_path)
    return _public_branding(record)


@router.delete("/admin/branding/login-background", response_model=PlatformBrandingPublic)
async def reset_login_background_video(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PlatformBrandingPublic:
    record = await _branding_record(session)
    previous_storage_path = record.login_background_video_storage_path
    record.login_background_video_source = "default"
    record.login_background_video_url = None
    record.login_background_video_storage_path = None
    await session.commit()
    await session.refresh(record)
    await _delete_replaced_upload(previous_storage_path)
    return _public_branding(record)
