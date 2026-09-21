from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_COVER_BYTES = 100 * 1024 * 1024
MAX_COVER_EDGE = 4096
MIN_COVER_EDGE = 64
AVATAR_EDGE = 512
ALLOWED_COVER_TYPES = {"image/jpeg", "image/png", "image/webp"}
# Phone cameras store HDR, portrait and burst shots as a JPEG carrying a second
# embedded picture (MPO), which Pillow reports as "MPO" rather than "JPEG".
ALLOWED_COVER_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
# Only these decode at reduced scale while reading (see _sampled).
SAMPLED_FORMATS = {"JPEG", "MPO"}
# 108MP and 200MP phone sensors exceed the old 100MP bound while still being
# ordinary JPEGs that decode in bounded memory once sampling is applied.
MAX_UPLOAD_PIXELS = 240_000_000
# Formats without a sampling decoder expand to their full frame, so they keep
# the previous, stricter bound.
MAX_UPLOAD_PIXELS_LOSSLESS = 100_000_000
# Peak decoded frame for sampled formats. The JPEG decoder only scales by whole
# powers of two, so this budget — not the upload size — is what bounds memory:
# 64MP is ~192MB of RGB, still below the 100MP (300MB) the previous limit allowed.
MAX_DECODE_PIXELS = 64_000_000
SAMPLING_STEPS = (1, 2, 4, 8)
Image.MAX_IMAGE_PIXELS = MAX_UPLOAD_PIXELS

HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif"}


class InvalidCoverImage(ValueError):
    pass


def _sampled(data: bytes) -> Image.Image:
    """Open bytes and let the decoder scale very large JPEG/MPO files down.

    libjpeg-turbo decodes those at 1/2, 1/4 or 1/8 scale in the DCT domain, so a
    240MP photo never expands to its full ~720MB RGB frame on the way to WebP.
    The divisor is chosen to stay within MAX_DECODE_PIXELS while keeping the
    frame at or above MAX_COVER_EDGE, so nothing a caller stores is undersized
    and ordinary photos are still decoded at full resolution.
    """
    image = Image.open(BytesIO(data))
    if image.format in SAMPLED_FORMATS:
        image.draft("RGB", _draft_target(image))
    return image


def _draft_target(image: Image.Image) -> tuple[int, int]:
    """Edge that makes the decoder land at the smallest step within budget.

    Pillow keeps the smaller of width//target and height//target, so the target
    is expressed in the file's own dimensions to select exactly `step`.
    """
    step = SAMPLING_STEPS[-1]
    for candidate in SAMPLING_STEPS:
        width, height = image.width // candidate, image.height // candidate
        if width * height <= MAX_DECODE_PIXELS:
            step = candidate
            break
    return (max(1, image.width // step), max(1, image.height // step))


def _heif_hint(data: bytes) -> bool:
    """Detect iPhone/AVIF containers Pillow cannot decode, for a clearer error."""
    return len(data) > 12 and data[4:8] == b"ftyp" and data[8:12].lower() in HEIF_BRANDS


def validate_uploaded_image(data: bytes) -> None:
    """Trust decoded contents, not the browser MIME label; bound decode memory."""
    if len(data) > MAX_COVER_BYTES:
        raise InvalidCoverImage("单张图片不能超过 100MB")
    try:
        with Image.open(BytesIO(data)) as source:
            limit = MAX_UPLOAD_PIXELS if source.format in SAMPLED_FORMATS else MAX_UPLOAD_PIXELS_LOSSLESS
            if source.width * source.height > limit:
                raise InvalidCoverImage(
                    f"图片像素过大，请将总像素缩小至 {limit / 100_000_000:g} 亿以内后上传"
                )
            if source.format not in ALLOWED_COVER_FORMATS:
                raise InvalidCoverImage("仅支持 JPG、PNG 或 WebP 图片")
            source.verify()
    except Image.DecompressionBombError as exc:
        raise InvalidCoverImage("图片像素过大，无法安全处理，请缩小分辨率后重试") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, InvalidCoverImage):
            raise
        if _heif_hint(data):
            raise InvalidCoverImage(
                "该图片是 HEIC/HEIF 等手机压缩格式，请先转为 JPG 或 PNG 后上传"
            ) from exc
        raise InvalidCoverImage("图片文件无效或已损坏") from exc


def _save_normalized_cover(data: bytes, target_dir: Path, *, webp_method: int = 6,
                           preserve_webp: bool = False) -> Path:
    already_normalized = False
    try:
        validate_uploaded_image(data)

        with _sampled(data) as source:
            already_normalized = (
                preserve_webp and source.format == "WEBP"
                and not getattr(source, "is_animated", False)
                and max(source.size) <= MAX_COVER_EDGE
                and source.getexif().get(274, 1) == 1
            )
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
        if already_normalized:
            # The browser encoded this image already; decoding above still
            # validates its pixels, but a second lossy encode adds no value.
            temporary.write_bytes(data)
        else:
            normalized.save(temporary, format="WEBP", quality=90, method=webp_method)
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
        validate_uploaded_image(data)

        with _sampled(data) as source:
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
        validate_uploaded_image(data)

        with _sampled(data) as source:
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
        webp_method=4,
        preserve_webp=True,
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
