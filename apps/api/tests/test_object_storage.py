from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services import object_storage as storage_module
from app.services.object_storage import (
    LocalObjectStorage,
    delete_media_file,
    materialize_media_file,
    media_signature,
    persist_media_file,
    valid_media_signature,
    validate_object_key,
)


class FakeRemoteStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def ensure_ready(self) -> None:
        return None

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = data

    async def put_file(self, key: str, source: Path, content_type: str) -> None:
        del content_type
        self.objects[key] = source.read_bytes()

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def download_file(self, key: str, target: Path) -> None:
        target.write_bytes(self.objects[key])

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        normalized = f"{prefix.rstrip('/')}/"
        for key in list(self.objects):
            if key.startswith(normalized):
                self.objects.pop(key)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_local_object_storage_round_trip(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    await storage.ensure_ready()
    key = "tenant-1/projects/project-1/files/source.txt"

    await storage.put_bytes(key, b"chapter one", "text/plain")

    assert await storage.get_bytes(key) == b"chapter one"
    await storage.delete(key)
    assert not storage.resolve(key).exists()


@pytest.mark.asyncio
async def test_local_object_storage_deletes_only_requested_prefix(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    await storage.ensure_ready()
    first = "tenant-1/projects/project-1/files/source.txt"
    second = "tenant-1/projects/project-2/files/source.txt"
    await storage.put_bytes(first, b"first", "text/plain")
    await storage.put_bytes(second, b"second", "text/plain")

    await storage.delete_prefix("tenant-1/projects/project-1")

    assert not storage.resolve(first).exists()
    assert await storage.get_bytes(second) == b"second"


def test_object_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)

    with pytest.raises(ValueError, match="对象存储路径无效"):
        validate_object_key("../private.txt")
    with pytest.raises(ValueError, match="对象存储路径无效"):
        storage.resolve(str(tmp_path.parent / "outside.txt"))


@pytest.mark.asyncio
async def test_remote_media_is_rehydrated_into_worker_cache(monkeypatch) -> None:
    remote = FakeRemoteStorage()
    monkeypatch.setattr(storage_module, "object_storage", lambda: remote)
    cache_path = (
        get_settings().uploads_root
        / "tenant-remote"
        / "projects"
        / "project-remote"
        / "videos"
        / "clip.mp4"
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"remote-video")

    key, media_url = await persist_media_file(cache_path, "video/mp4")
    cache_path.unlink()
    hydrated = await materialize_media_file(key)

    assert media_url == f"/uploads/{key}"
    assert hydrated == cache_path
    assert hydrated.read_bytes() == b"remote-video"
    await delete_media_file(key, hydrated)
    assert key not in remote.objects
    assert not hydrated.exists()


def test_media_signature_is_path_bound() -> None:
    key = "tenant-1/projects/project-1/videos/clip.mp4"
    signature = media_signature(key)

    assert valid_media_signature(key, signature)
    assert not valid_media_signature("tenant-1/projects/project-1/videos/other.mp4", signature)
    assert not valid_media_signature(key, None)
