from __future__ import annotations

import hashlib
import hmac
import shutil
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings


class ObjectStorage(Protocol):
    async def ensure_ready(self) -> None: ...

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...

    async def put_file(self, key: str, source: Path, content_type: str) -> None: ...

    async def get_bytes(self, key: str) -> bytes: ...

    async def download_file(self, key: str, target: Path) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    async def close(self) -> None: ...


def validate_object_key(key: str) -> str:
    normalized = PurePosixPath(key.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        raise ValueError("对象存储路径无效")
    return normalized.as_posix()


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, key: str) -> Path:
        candidate = Path(key)
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.root / validate_object_key(key)).resolve()
        )
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError("对象存储路径无效") from error
        return target

    async def ensure_ready(self) -> None:
        await run_in_threadpool(self.root.mkdir, parents=True, exist_ok=True)

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        target = self.resolve(key)

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f"{target.suffix}.uploading")
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

        await run_in_threadpool(write)

    async def put_file(self, key: str, source: Path, content_type: str) -> None:
        del content_type
        target = self.resolve(key)
        if source.resolve() == target:
            return

        def copy() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f"{target.suffix}.{uuid4().hex}.uploading")
            try:
                shutil.copyfile(source, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

        await run_in_threadpool(copy)

    async def get_bytes(self, key: str) -> bytes:
        return await run_in_threadpool(self.resolve(key).read_bytes)

    async def download_file(self, key: str, target: Path) -> None:
        source = self.resolve(key)
        if source == target.resolve():
            if not source.is_file():
                raise FileNotFoundError(source)
            return
        await run_in_threadpool(shutil.copyfile, source, target)

    async def delete(self, key: str) -> None:
        await run_in_threadpool(self.resolve(key).unlink, missing_ok=True)

    async def delete_prefix(self, prefix: str) -> None:
        target = self.resolve(validate_object_key(prefix))
        await run_in_threadpool(shutil.rmtree, target, True)

    async def close(self) -> None:
        return None


class S3ObjectStorage:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.s3_access_key_id or not settings.s3_secret_access_key:
            raise RuntimeError("S3 对象存储缺少访问凭据")
        try:
            import aioboto3
        except ImportError as error:
            raise RuntimeError("S3 对象存储依赖未安装") from error
        self._session = aioboto3.Session()
        self._client_options = {
            "service_name": "s3",
            "endpoint_url": settings.s3_endpoint_url,
            "aws_access_key_id": settings.s3_access_key_id,
            "aws_secret_access_key": settings.s3_secret_access_key,
            "region_name": settings.s3_region,
        }
        self.bucket = settings.s3_bucket
        self.auto_create = settings.s3_auto_create_bucket

    def client(self):
        return self._session.client(**self._client_options)

    async def ensure_ready(self) -> None:
        async with self.client() as client:
            try:
                await client.head_bucket(Bucket=self.bucket)
            except client.exceptions.ClientError:
                if not self.auto_create:
                    raise RuntimeError(f"S3 存储桶不可用：{self.bucket}") from None
                await client.create_bucket(Bucket=self.bucket)

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        async with self.client() as client:
            await client.put_object(
                Bucket=self.bucket,
                Key=validate_object_key(key),
                Body=data,
                ContentType=content_type,
            )

    async def put_file(self, key: str, source: Path, content_type: str) -> None:
        async with self.client() as client:
            await client.upload_file(
                str(source),
                self.bucket,
                validate_object_key(key),
                ExtraArgs={"ContentType": content_type},
            )

    async def get_bytes(self, key: str) -> bytes:
        async with self.client() as client:
            response = await client.get_object(Bucket=self.bucket, Key=validate_object_key(key))
            return await response["Body"].read()

    async def download_file(self, key: str, target: Path) -> None:
        async with self.client() as client:
            await client.download_file(self.bucket, validate_object_key(key), str(target))

    async def delete(self, key: str) -> None:
        async with self.client() as client:
            await client.delete_object(Bucket=self.bucket, Key=validate_object_key(key))

    async def delete_prefix(self, prefix: str) -> None:
        normalized = f"{validate_object_key(prefix).rstrip('/')}/"
        continuation_token: str | None = None
        async with self.client() as client:
            while True:
                request = {"Bucket": self.bucket, "Prefix": normalized}
                if continuation_token:
                    request["ContinuationToken"] = continuation_token
                response = await client.list_objects_v2(**request)
                objects = [{"Key": item["Key"]} for item in response.get("Contents", [])]
                if objects:
                    await client.delete_objects(
                        Bucket=self.bucket,
                        Delete={"Objects": objects, "Quiet": True},
                    )
                if not response.get("IsTruncated"):
                    return
                continuation_token = response.get("NextContinuationToken")
                if not continuation_token:
                    return

    async def close(self) -> None:
        return None


@lru_cache
def object_storage() -> ObjectStorage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        return S3ObjectStorage()
    return LocalObjectStorage(settings.uploads_root)


def object_key_for_local_path(path: Path) -> str:
    root = get_settings().uploads_root.resolve()
    target = path.resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError("媒体缓存路径不在上传目录中") from error


def public_media_url(key: str) -> str:
    normalized = validate_object_key(key)
    url = f"/uploads/{normalized}"
    if get_settings().require_signed_media_urls:
        return f"{url}?signature={media_signature(normalized)}"
    return url


def object_key_from_media_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlsplit(url).path
    prefix = "/uploads/"
    if not path.startswith(prefix):
        return None
    try:
        return validate_object_key(path.removeprefix(prefix))
    except ValueError:
        return None


def media_signature(key: str) -> str:
    normalized = validate_object_key(key)
    return hmac.new(
        get_settings().media_signing_secret.encode(),
        f"cineforge-media:{normalized}".encode(),
        hashlib.sha256,
    ).hexdigest()


def valid_media_signature(key: str, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(media_signature(key), signature)


async def persist_media_file(path: Path, content_type: str) -> tuple[str, str]:
    key = object_key_for_local_path(path)
    await object_storage().put_file(key, path, content_type)
    return key, public_media_url(key)


async def materialize_media_file(storage_path: str) -> Path:
    settings = get_settings()
    root = settings.uploads_root.resolve()
    candidate = Path(storage_path)
    if candidate.is_absolute():
        target = candidate.resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError("媒体路径不在上传目录中") from error
        if not target.is_file():
            raise FileNotFoundError(target)
        return target

    key = validate_object_key(storage_path)
    target = (root / key).resolve()
    if target.is_file():
        return target
    await run_in_threadpool(target.parent.mkdir, parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.{uuid4().hex}.hydrating")
    try:
        await object_storage().download_file(key, temporary)
        await run_in_threadpool(temporary.replace, target)
    finally:
        await run_in_threadpool(temporary.unlink, missing_ok=True)
    return target


async def delete_media_file(storage_path: str, cached_path: Path | None = None) -> None:
    candidate = Path(storage_path)
    if candidate.is_absolute():
        await run_in_threadpool(candidate.unlink, missing_ok=True)
        return
    await object_storage().delete(storage_path)
    if cached_path is not None and not isinstance(object_storage(), LocalObjectStorage):
        await run_in_threadpool(cached_path.unlink, missing_ok=True)


async def delete_media_prefix(prefix: str) -> None:
    await object_storage().delete_prefix(prefix)


async def close_object_storage() -> None:
    storage = object_storage()
    await storage.close()
    object_storage.cache_clear()
