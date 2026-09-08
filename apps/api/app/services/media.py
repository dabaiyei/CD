from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_COVER_BYTES = 8 * 1024 * 1024
MAX_COVER_EDGE = 4096
MIN_COVER_EDGE = 64
AVATAR_EDGE = 512
ALLOWED_COVER_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_COVER_FORMATS = {"JPEG", "PNG", "WEBP"}

Image.MAX_IMAGE_PIXELS = 40_000_000


class InvalidCoverImage(ValueError):
    pass


def _save_normalized_cover(data: bytes, target_dir: Path) -> Path:
    try:
        with Image.open(BytesIO(data)) as source:
            source.verify()
            if source.format not in ALLOWED_COVER_FORMATS:
                raise InvalidCoverImage("仅支持 JPG、PNG 或 WebP 图片")

        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if min(image.size) < MIN_COVER_EDGE:
                raise InvalidCoverImage("封面尺寸过小，宽高均需至少 64 像素")
            image.thumbnail((MAX_COVER_EDGE, MAX_COVER_EDGE), Image.Resampling.LANCZOS)
            normalized = image.convert("RGBA" if image.has_transparency_data else "RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, InvalidCoverImage):
            raise
        raise InvalidCoverImage("图片文件无效或已损坏") from exc

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid4()}.webp"
    temporary = target.with_suffix(".tmp")
    try:
        normalized.save(temporary, format="WEBP", quality=90, method=6)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

    return target


def save_project_cover(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    project_id: str,
) -> tuple[str, Path]:
    target = _save_normalized_cover(data, uploads_root / tenant_id / "projects" / project_id)
    relative = target.relative_to(uploads_root).as_posix()
    return f"/uploads/{relative}", target


def save_handbook_cover(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    handbook_id: str,
) -> tuple[str, Path]:
    target = _save_normalized_cover(data, uploads_root / tenant_id / "handbooks" / handbook_id)
    relative = target.relative_to(uploads_root).as_posix()
    return f"/uploads/{relative}", target


def save_user_avatar(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    user_id: str,
) -> tuple[str, Path]:
    try:
        with Image.open(BytesIO(data)) as source:
            source.verify()
            if source.format not in ALLOWED_COVER_FORMATS:
                raise InvalidCoverImage("仅支持 JPG、PNG 或 WebP 图片")

        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if min(image.size) < MIN_COVER_EDGE:
                raise InvalidCoverImage("头像尺寸过小，宽高均需至少 64 像素")
            if image.has_transparency_data:
                rgba = image.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, "#ffffff")
                flattened.paste(rgba, mask=rgba.getchannel("A"))
            else:
                flattened = image.convert("RGB")
            normalized = ImageOps.fit(
                flattened,
                (AVATAR_EDGE, AVATAR_EDGE),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, InvalidCoverImage):
            raise
        raise InvalidCoverImage("图片文件无效或已损坏") from exc

    target_dir = uploads_root / tenant_id / "users" / user_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"avatar-{uuid4()}.webp"
    temporary = target.with_suffix(".tmp")
    try:
        normalized.save(temporary, format="WEBP", quality=90, method=6)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

    relative = target.relative_to(uploads_root).as_posix()
    return f"/uploads/{relative}", target


def save_asset_image(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    project_id: str | None,
    asset_id: str,
) -> tuple[str, Path]:
    try:
        with Image.open(BytesIO(data)) as source:
            source.verify()
            if source.format not in ALLOWED_COVER_FORMATS:
                raise InvalidCoverImage("仅支持 JPG、PNG 或 WebP 图片")

        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if min(image.size) < MIN_COVER_EDGE:
                raise InvalidCoverImage("资产图片尺寸过小，宽高均需至少 64 像素")
            image.thumbnail((MAX_COVER_EDGE, MAX_COVER_EDGE), Image.Resampling.LANCZOS)
            normalized = image.convert("RGBA" if image.has_transparency_data else "RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, InvalidCoverImage):
            raise
        raise InvalidCoverImage("图片文件无效或已损坏") from exc

    target_dir = (
        uploads_root / tenant_id / "projects" / project_id / "assets"
        if project_id
        else uploads_root / tenant_id / "global-assets"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{asset_id}-{uuid4()}.webp"
    temporary = target.with_suffix(".tmp")
    try:
        normalized.save(temporary, format="WEBP", quality=92, method=6)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

    relative = target.relative_to(uploads_root).as_posix()
    return f"/uploads/{relative}", target


def save_asset_reference_audio(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    project_id: str | None,
    asset_id: str,
    content_type: str | None,
) -> tuple[str, Path, str]:
    if not data:
        raise ValueError("参考音频文件为空")
    formats = {
        "audio/mpeg": (".mp3", "audio/mpeg"),
        "audio/wav": (".wav", "audio/wav"),
        "audio/x-wav": (".wav", "audio/wav"),
        "audio/ogg": (".ogg", "audio/ogg"),
        "audio/webm": (".webm", "audio/webm"),
        "audio/flac": (".flac", "audio/flac"),
        "audio/mp4": (".m4a", "audio/mp4"),
        "audio/aac": (".aac", "audio/aac"),
    }
    suffix, mime_type = formats.get(content_type or "", (".mp3", "audio/mpeg"))
    target_dir = (
        uploads_root / tenant_id / "projects" / project_id / "assets" / "reference-audio"
        if project_id
        else uploads_root / tenant_id / "global-assets" / "reference-audio"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{asset_id}-{uuid4()}{suffix}"
    temporary = target.with_suffix(f"{suffix}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    relative = target.relative_to(uploads_root).as_posix()
    return f"/uploads/{relative}", target, mime_type


def save_agent_chat_image(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    project_id: str,
) -> Path:
    return _save_normalized_cover(
        data,
        uploads_root / tenant_id / "projects" / project_id / "agent-attachments",
    )


def save_project_video(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    project_id: str,
    clip_id: str,
    content_type: str | None,
) -> tuple[str, Path, str]:
    if not data:
        raise ValueError("视频文件为空")
    suffix = ".webm" if content_type == "video/webm" or data[:4] == b"\x1aE\xdf\xa3" else ".mp4"
    mime_type = "video/webm" if suffix == ".webm" else "video/mp4"
    target_dir = uploads_root / tenant_id / "projects" / project_id / "videos"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{clip_id}-{uuid4()}{suffix}"
    temporary = target.with_suffix(f"{suffix}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    relative = target.relative_to(uploads_root).as_posix()
    return f"/uploads/{relative}", target, mime_type


def save_project_audio(
    data: bytes,
    *,
    uploads_root: Path,
    tenant_id: str,
    project_id: str,
    clip_id: str,
    content_type: str | None,
) -> tuple[str, Path, str]:
    if not data:
        raise ValueError("音频文件为空")
    formats = {
        "audio/wav": (".wav", "audio/wav"),
        "audio/x-wav": (".wav", "audio/wav"),
        "audio/ogg": (".ogg", "audio/ogg"),
        "audio/webm": (".webm", "audio/webm"),
        "audio/flac": (".flac", "audio/flac"),
        "audio/mp4": (".m4a", "audio/mp4"),
        "audio/aac": (".aac", "audio/aac"),
    }
    suffix, mime_type = formats.get(content_type or "", (".mp3", "audio/mpeg"))
    target_dir = uploads_root / tenant_id / "projects" / project_id / "audio"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{clip_id}-{uuid4()}{suffix}"
    temporary = target.with_suffix(f"{suffix}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    relative = target.relative_to(uploads_root).as_posix()
    return f"/uploads/{relative}", target, mime_type
