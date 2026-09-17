"""Portable, password-encrypted site snapshots. No SQL from an upload is executed."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import uuid
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import select, inspect, update, delete, insert

from app.core.config import get_settings
from app.core.security import SecretBox
from app.db import models  # register all tables
from app.db.session import Base, engine
from app.services.object_storage import object_storage, public_media_url, S3ObjectStorage
from app.services import site_maintenance as maintenance

MAGIC = b"CFBAK001"
CHUNK = 1024 * 1024
FORMAT = 1
JOBS: set[asyncio.Task] = set()


def filesystem_path(path: Path) -> Path:
    path = path.resolve()
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        value = str(path)
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return path


def backup_root() -> Path:
    path = filesystem_path(get_settings().site_backup_root)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def job_dir(job_id: str) -> Path:
    # Identifiers, never user-supplied filenames, select filesystem objects.
    return backup_root() / str(uuid.UUID(job_id))


def state(job_id: str) -> dict:
    return json.loads((job_dir(job_id) / "job.json").read_text(encoding="utf-8"))


def save_state(job_id: str, **updates) -> dict:
    folder = job_dir(job_id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "job.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update(updates, id=job_id, updated_at=datetime.now(timezone.utc).isoformat())
    tmp = folder / "job.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data


def new_job(kind: str, user_id: str, **extra) -> dict:
    job_id = str(uuid.uuid4())
    return save_state(job_id, **{ "kind": kind, "user_id": user_id, "status": "queued", "progress": 0,
                      "message": "等待执行", "created_at": datetime.now(timezone.utc).isoformat(), **extra})


def list_jobs() -> list[dict]:
    return sorted([json.loads(p.read_text(encoding="utf-8")) for p in backup_root().glob("*/job.json")],
                  key=lambda item: item["created_at"], reverse=True)


def launch(coro):
    task = asyncio.create_task(coro)
    JOBS.add(task)
    task.add_done_callback(JOBS.discard)


def encode(value):
    # Tagged pairs for every value avoid collisions with arbitrary user JSON.
    if value is None: return ["null", None]
    if isinstance(value, bool): return ["bool", value]
    if isinstance(value, datetime): return ["datetime", value.isoformat()]
    if isinstance(value, date): return ["date", value.isoformat()]
    if isinstance(value, Decimal): return ["decimal", str(value)]
    if isinstance(value, bytes): return ["bytes", base64.b64encode(value).decode()]
    if isinstance(value, str): return ["str", value]
    if isinstance(value, int): return ["int", value]
    if isinstance(value, float): return ["float", value]
    if isinstance(value, dict): return ["dict", {k: encode(v) for k, v in value.items()}]
    if isinstance(value, (tuple, list)): return ["list", [encode(v) for v in value]]
    if hasattr(value, "tolist"): return encode(value.tolist())
    raise ValueError(f"无法备份的数据类型：{type(value).__name__}")


def decode(pair):
    kind, value = pair
    if kind in {"null", "bool", "str", "int", "float"}: return value
    if kind == "datetime": return datetime.fromisoformat(value)
    if kind == "date": return date.fromisoformat(value)
    if kind == "decimal": return Decimal(value)
    if kind == "bytes": return base64.b64decode(value, validate=True)
    if kind == "dict": return {k: decode(v) for k, v in value.items()}
    if kind == "list": return [decode(v) for v in value]
    raise ValueError("备份数据类型不受支持")


def schema() -> dict:
    return {name: {c.name: str(c.type) for c in table.columns}
            for name, table in sorted(Base.metadata.tables.items())}


def data_roots() -> dict[str, Path]:
    settings = get_settings()
    roots = {"uploads": settings.uploads_root.resolve(), "skills": settings.skills_root.resolve(),
             "runtime": settings.site_backup_runtime_root.resolve()}
    paths = [*roots.values(), settings.site_backup_root.resolve()]
    for index, path in enumerate(paths):
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError("备份数据目录不能指向磁盘根目录或用户主目录")
        if any(path.is_relative_to(other) or other.is_relative_to(path) for other in paths[index + 1:]):
            raise ValueError("备份目录与各数据目录必须独立，不能相互包含")
    return roots


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("备份包含非法路径")
    if any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in name.split("/")):
        raise ValueError("备份包含非法路径")
    if any(p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1,10)], *[f"LPT{i}" for i in range(1,10)]} for p in path.parts):
        raise ValueError("备份包含不可移植的文件名")
    return path.as_posix()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""): h.update(block)
    return h.hexdigest()


def crypt_file(source: Path, target: Path, password: str, *, decrypt=False):
    """Streaming AES-256-GCM; authenticate fully before reading the ZIP."""
    try:
        with source.open("rb") as inp, target.open("wb") as out:
            if decrypt:
                header = inp.read(36)
                if len(header) != 36 or header[:8] != MAGIC: raise ValueError("不是有效的站点备份文件")
                salt, nonce = header[8:24], header[24:36]
                length = source.stat().st_size - 52
                if length < 0: raise ValueError("备份文件不完整")
                inp.seek(-16, 2)
                tag = inp.read(16)
                inp.seek(36)
            else:
                salt, nonce = os.urandom(16), os.urandom(12)
                header = MAGIC + salt + nonce
                length = source.stat().st_size
                out.write(header)
            key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000, dklen=32)
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag) if decrypt else modes.GCM(nonce))
            ctx = cipher.decryptor() if decrypt else cipher.encryptor()
            ctx.authenticate_additional_data(header)
            while length:
                block = inp.read(min(CHUNK, length))
                if not block: raise ValueError("备份文件不完整")
                length -= len(block)
                out.write(ctx.update(block))
            out.write(ctx.finalize())
            if not decrypt: out.write(ctx.tag)
    except InvalidTag:
        target.unlink(missing_ok=True)
        raise ValueError("备份密码错误或文件已损坏") from None
    except BaseException:
        target.unlink(missing_ok=True)
        raise


async def export_tree(destination: Path):
    for name, source in data_roots().items():
        if not source.is_dir():
            raise ValueError(f"{name} 数据目录不可读，请检查本地路径或 Docker 数据卷挂载")
        target = destination / "files" / name
        target.mkdir(parents=True)
        def copy():
            for path in source.rglob("*"):
                if path.is_symlink(): raise ValueError("数据目录包含符号链接，无法保证完整备份")
                if path.is_file():
                    relative = safe_name(path.relative_to(source).as_posix())
                    out = target / relative
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("rb") as inp, out.open("wb") as handle:
                        shutil.copyfileobj(inp, handle, CHUNK)
        await asyncio.to_thread(copy)
    store = object_storage()
    if isinstance(store, S3ObjectStorage):
        async with store.client() as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=store.bucket):
                for item in page.get("Contents", []):
                    key = safe_name(item["Key"])
                    out = destination / "files/uploads" / key
                    out.parent.mkdir(parents=True, exist_ok=True)
                    await client.download_file(store.bucket, key, str(out))


async def snapshot(destination: Path, job_id: str) -> dict:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = destination / "database"
    db.mkdir()
    counts = {}
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        unknown = set(tables) - set(Base.metadata.tables) - {"alembic_version"}
        if unknown: raise ValueError("数据库包含未识别业务表，拒绝生成不完整备份")
        for name, table in Base.metadata.tables.items():
            count = 0
            # Fetch in bounded batches instead of loading all conversations into RAM.
            rows = await conn.stream(select(table))
            with (db / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
                async for batch in rows.mappings().partitions(250):
                    lines = "".join(json.dumps(encode(dict(row)), ensure_ascii=False) + "\n" for row in batch)
                    await asyncio.to_thread(handle.write, lines)
                    count += len(batch)
            counts[name] = count
    save_state(job_id, progress=25, message="正在备份媒体、Skills 和会话记忆")
    await export_tree(destination)
    settings = get_settings()
    (destination / "secrets.json").write_text(json.dumps({
        "credential_encryption_secret": settings.credential_encryption_secret,
    }), encoding="utf-8")
    manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                "schema": schema(), "counts": counts,
                "database": engine.dialect.name,
                "roots": {key: str(value) for key, value in data_roots().items()},
                "files": {}}
    def inventory():
        total = 0
        for path in destination.rglob("*"):
            if path.is_file():
                size = path.stat().st_size
                total += size
                if total > settings.site_backup_max_bytes: raise ValueError("备份超出站点配置的容量上限")
                manifest["files"][path.relative_to(destination).as_posix()] = {"size": size, "sha256": digest(path)}
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    await asyncio.to_thread(inventory)
    return manifest


def pack(folder: Path, output: Path, password: str):
    raw = output.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for path in folder.rglob("*"):
                if path.is_file(): archive.write(path, path.relative_to(folder).as_posix())
        crypt_file(raw, output, password)
    finally:
        raw.unlink(missing_ok=True)


def ordered_tables():
    # Nullable cycles (chapter -> active script -> chapter, task -> child, etc.)
    # are restored in a second pass. Non-null foreign keys determine insertion.
    remaining = dict(Base.metadata.tables)
    ordered = []
    while remaining:
        ready = [t for t in remaining.values() if not any(
            fk.column.table.name in remaining and fk.column.table.name != t.name
            for c in t.columns if not c.nullable for fk in c.foreign_keys
        )]
        if not ready: raise ValueError("数据库存在无法安全恢复的非空循环引用")
        for table in ready:
            ordered.append(table)
            del remaining[table.name]
    return ordered


def relocated(value, manifest):
    if isinstance(value, dict): return {k: relocated(v, manifest) for k, v in value.items()}
    if isinstance(value, list): return [relocated(v, manifest) for v in value]
    if not isinstance(value, str): return value
    for name, target in data_roots().items():
        old = manifest["roots"][name]
        # Cover embedded manifests and runtime paths as well as DB path fields.
        value = value.replace(old.replace("\\", "/"), target.as_posix()).replace(old, str(target))
    # Stored URLs may carry the source server's media signature. Sign local
    # object URLs with the destination key; keep third-party URLs untouched.
    if value.startswith("/uploads/"):
        from urllib.parse import urlsplit
        value = public_media_url(urlsplit(value).path.removeprefix("/uploads/"))
    return value


def rows_for(folder, table, manifest):
    source_secret = json.loads((folder / "secrets.json").read_text(encoding="utf-8"))["credential_encryption_secret"]
    source_box, target_box = SecretBox(source_secret), SecretBox()
    with (folder / "database" / f"{table.name}.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = relocated(decode(json.loads(line)), manifest)
            for field in ("encrypted_api_key", "encrypted_credentials"):
                if row.get(field): row[field] = target_box.encrypt(source_box.decrypt(row[field]))
            yield row


async def restore_database(conn, folder, manifest):
    tables = ordered_tables()
    for table in tables:
        nullable = {c.name: None for c in table.columns if c.nullable and c.foreign_keys}
        if nullable: await conn.execute(update(table).values(**nullable))
    for table in reversed(tables): await conn.execute(delete(table))
    for table in tables:
        nullable = {c.name for c in table.columns if c.nullable and c.foreign_keys}
        batch = []
        for row in rows_for(folder, table, manifest):
            batch.append({k: None if k in nullable else v for k, v in row.items()})
            if len(batch) == 200:
                await conn.execute(insert(table), batch)
                batch = []
        if batch: await conn.execute(insert(table), batch)
    for table in tables:
        nullable = {c.name for c in table.columns if c.nullable and c.foreign_keys}
        if not nullable: continue
        for row in rows_for(folder, table, manifest):
            values = {key: row[key] for key in nullable if row[key] is not None}
            if values:
                stmt = update(table)
                for c in table.primary_key.columns: stmt = stmt.where(c == row[c.name])
                await conn.execute(stmt.values(**values))


async def install_files(folder: Path):
    def install_local():
        for name, target in data_roots().items():
            source = folder / "files" / name
            target.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or source.is_symlink(): raise ValueError("恢复目录不能是符号链接")
            # Never rename/delete a mount root. Work only with validated relative
            # file keys, keeping a complete safety snapshot until commit.
            expected = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
            for path in target.rglob("*"):
                if path.is_symlink(): raise ValueError("目标数据目录包含符号链接")
                if path.is_file() and path.relative_to(target).as_posix() not in expected:
                    path.unlink()
            for relative in expected:
                destination = target / safe_name(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with (source / relative).open("rb") as inp, destination.open("wb") as out:
                    shutil.copyfileobj(inp, out, CHUNK)
    await asyncio.to_thread(install_local)
    store = object_storage()
    if isinstance(store, S3ObjectStorage):
        source = folder / "files/uploads"
        expected = set()
        async with store.client() as client:
            for path in source.rglob("*"):
                if path.is_file():
                    key = safe_name(path.relative_to(source).as_posix())
                    expected.add(key)
                    await client.upload_file(str(path), store.bucket, key,
                        ExtraArgs={"ContentType": mimetypes.guess_type(key)[0] or "application/octet-stream"})
            async for page in client.get_paginator("list_objects_v2").paginate(Bucket=store.bucket):
                obsolete = [{"Key": x["Key"]} for x in page.get("Contents", []) if x["Key"] not in expected]
                if obsolete:
                    result = await client.delete_objects(Bucket=store.bucket, Delete={"Objects": obsolete})
                    if result.get("Errors"): raise ValueError("部分旧媒体无法清理，还原已中止")


async def apply_snapshot(folder, manifest):
    # File changes happen while a database transaction is open; any failure
    # rolls back DB writes. The caller restores files from the safety snapshot.
    async with engine.begin() as conn:
        await restore_database(conn, folder, manifest)
        await install_files(folder)


def summary(manifest):
    return {"created_at": manifest["created_at"], "database": manifest["database"],
            "tables": len(manifest["counts"]), "records": sum(manifest["counts"].values()),
            "counts": manifest["counts"], "files": len(manifest["files"]),
            "bytes": sum(x["size"] for x in manifest["files"].values())}


def remove_staging(path: Path):
    # Only private, generated subdirectories of this job store may be removed.
    if path.resolve().is_relative_to(backup_root()) and len(path.relative_to(backup_root()).parts) >= 2:
        shutil.rmtree(path, ignore_errors=True)


async def create_backup(job_id: str, password: str):
    stage = job_dir(job_id) / "staging"
    entered = False
    try:
        save_state(job_id, status="running", progress=5, message="等待当前请求结束，暂停新任务")
        await maintenance.enter(job_id)
        entered = True
        manifest = await snapshot(stage, job_id)
        save_state(job_id, progress=70, message="正在加密备份包")
        await asyncio.to_thread(pack, stage, job_dir(job_id) / "site.cfbackup", password)
        save_state(job_id, status="completed", progress=100, message="全站备份完成",
                   summary=summary(manifest), downloadable=True)
    except Exception:
        # Do not expose credentials, SQL parameters, or content in error output.
        save_state(job_id, status="failed", message="备份失败：请检查是否仍有运行任务、磁盘空间及存储目录权限", progress=0)
    finally:
        await asyncio.to_thread(remove_staging, stage)
        if entered: maintenance.leave(job_id)


async def validate_backup(job_id, password):
    stage = job_dir(job_id) / "validation"
    try:
        save_state(job_id, status="validating", progress=10, message="正在解密并校验全部文件")
        manifest = await asyncio.to_thread(unpack, job_dir(job_id) / "site.cfbackup", stage, password)
        fingerprint = await asyncio.to_thread(digest, job_dir(job_id) / "site.cfbackup")
        save_state(job_id, status="ready", progress=100, message="校验通过，可以还原",
                   summary=summary(manifest), fingerprint=fingerprint, downloadable=True)
    except Exception:
        save_state(job_id, status="invalid", progress=0,
                   message="校验失败：密码错误、文件损坏、容量超限或程序数据结构不一致")
    finally:
        await asyncio.to_thread(remove_staging, stage)


async def restore_backup(job_id, source_id, password):
    folder = job_dir(job_id)
    stage, rollback = folder / "incoming", folder / "rollback"
    entered = False
    mutated = False
    keep_lock = False
    try:
        save_state(job_id, status="running", progress=5, message="重新校验备份")
        manifest = await asyncio.to_thread(unpack, job_dir(source_id) / "site.cfbackup", stage, password)
        await maintenance.enter(job_id)
        entered = True
        save_state(job_id, message="保存还原前的全站恢复点", progress=15)
        before = await snapshot(rollback, job_id)
        await asyncio.to_thread(pack, rollback, folder / "site.cfbackup", password)
        save_state(job_id, downloadable=True, summary=summary(before), phase="applying",
                   message="正在还原数据库、媒体及记忆", progress=55)
        mutated = True
        await apply_snapshot(stage, manifest)
        save_state(job_id, phase="committed", status="completed", progress=100,
                   message="站点还原成功，请重新登录；本记录可下载还原前的恢复点")
    except asyncio.CancelledError:
        keep_lock = mutated
        raise
    except Exception:
        if mutated:
            try:
                before = json.loads((rollback / "manifest.json").read_text(encoding="utf-8"))
                await apply_snapshot(rollback, before)
                save_state(job_id, status="failed", phase="rolled_back", message="还原失败，已恢复到操作前状态")
            except Exception:
                keep_lock = True
                save_state(job_id, status="recovery_required", message="回滚未完成，站点保持维护模式；请重试恢复点回滚")
        else:
            save_state(job_id, status="failed", message="还原未开始：校验、维护等待或恢复点创建失败，原站点未被覆盖")
    finally:
        await asyncio.to_thread(remove_staging, stage)
        if not keep_lock:
            await asyncio.to_thread(remove_staging, rollback)
            if entered: maintenance.leave(job_id)


async def recover_interrupted():
    # Fail closed if an API crash interrupted restoration. Roll back before
    # allowing worker claims or user writes, using the private recovery point.
    for job in list_jobs():
        if job["status"] not in {"queued", "running", "validating", "uploading", "recovery_required"}: continue
        job_id = job["id"]
        rollback = job_dir(job_id) / "rollback"
        if job.get("phase") == "applying" or job["status"] == "recovery_required":
            try:
                before = json.loads((rollback / "manifest.json").read_text(encoding="utf-8"))
                await apply_snapshot(rollback, before)
                save_state(job_id, status="failed", phase="rolled_back", message="重启后已恢复还原前状态")
            except Exception:
                save_state(job_id, status="recovery_required", message="站点还原中断，自动回滚未完成；请检查磁盘/对象存储后重试")
                continue
        else:
            save_state(job_id, status="failed", message="服务重启导致操作中断，请重新执行")
            if job["status"] == "uploading":
                (job_dir(job_id) / "site.cfbackup").unlink(missing_ok=True)
        maintenance.leave(job_id)
        for name in ("staging", "incoming", "validation", "rollback"):
            await asyncio.to_thread(remove_staging, job_dir(job_id) / name)
            (job_dir(job_id) / f"{name}.zip.tmp").unlink(missing_ok=True)
        (job_dir(job_id) / "site.zip.tmp").unlink(missing_ok=True)


def unpack(source: Path, destination: Path, password: str) -> dict:
    destination = filesystem_path(destination)
    raw = destination.parent / f"{destination.name}.zip.tmp"
    destination.mkdir(parents=True, mode=0o700)
    try:
        crypt_file(source, raw, password, decrypt=True)
        with zipfile.ZipFile(raw) as archive:
            items = archive.infolist()
            names = [safe_name(item.filename) for item in items]
            if len(set(n.casefold() for n in names)) != len(names): raise ValueError("备份包含重复路径")
            if len(items) > 1_000_000 or sum(i.file_size for i in items) > get_settings().site_backup_max_bytes:
                raise ValueError("备份解压后超过容量限制")
            if "manifest.json" not in names or archive.getinfo("manifest.json").file_size > 64 * CHUNK:
                raise ValueError("备份清单无效")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != FORMAT or manifest.get("schema") != schema():
                raise ValueError("备份与当前程序的数据结构不一致，请先使用相同版本程序")
            if set(names) != set(manifest["files"]) | {"manifest.json"}: raise ValueError("备份文件清单不一致")
            for item, name in zip(items, names, strict=True):
                if stat.S_ISLNK(item.external_attr >> 16) or item.is_dir(): raise ValueError("备份包含非法链接或目录项")
                if name != "manifest.json" and not (name == "secrets.json" or name.startswith(("database/", "files/uploads/", "files/skills/", "files/runtime/"))):
                    raise ValueError("备份含有未支持的文件")
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as inp, path.open("wb") as out:
                    shutil.copyfileobj(inp, out, CHUNK)
                if name != "manifest.json":
                    expected = manifest["files"][name]
                    if path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
                        raise ValueError("备份文件完整性校验失败")
        for name in schema():
            path = destination / "database" / f"{name}.jsonl"
            count = 0
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = decode(json.loads(line))
                    if set(row) != set(schema()[name]): raise ValueError("备份数据列不一致")
                    count += 1
            if count != manifest["counts"][name]: raise ValueError("备份记录数量不一致")
        return manifest
    finally:
        raw.unlink(missing_ok=True)
