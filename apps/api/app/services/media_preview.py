"""Small cached previews without changing source media or its access policy."""

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

_preview_slots = asyncio.Semaphore(2)


def _create_preview(source: Path, edge: int) -> Path:
    stat = source.stat()
    identity = f"{source.name}:{stat.st_mtime_ns}:{stat.st_size}:{edge}"
    target = source.parent / ".previews" / (hashlib.sha256(identity.encode()).hexdigest() + ".webp")
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{uuid4().hex}.tmp")
    try:
        with Image.open(source) as image:
            image.draft("RGB", (edge, edge))
            preview = ImageOps.exif_transpose(image)
            preview.thumbnail((edge, edge))
            preview.convert("RGB").save(temporary, format="WEBP", quality=78)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


async def media_preview(source: Path, edge: int) -> Path:
    async with _preview_slots:
        return await asyncio.to_thread(_create_preview, source, edge)
