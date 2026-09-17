from __future__ import annotations

import asyncio
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, func, event
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.security import SecretBox
from app.db.session import Base, engine as source_engine
from app.db.models import Provider
from app.services import site_backup as backup
from app.services import site_maintenance as gate


def test_tagged_data_is_lossless():
    original = {"text": "中文对话\n第二行", "amount": Decimal("125.25"),
                "date": datetime.now(timezone.utc), "json": {"$cf_type": "datetime"},
                "blob": b"\x00\xff", "list": [None, True, 0, 1.25]}
    assert backup.decode(json.loads(json.dumps(backup.encode(original)))) == original


def test_archive_authentication_and_path_traversal(tmp_path):
    raw, encrypted = tmp_path / "bad.zip", tmp_path / "bad.cfbackup"
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("../escape.txt", "cannot write outside staging")
    backup.crypt_file(raw, encrypted, "test-password-1234")
    with pytest.raises(ValueError, match="密码错误"):
        backup.unpack(encrypted, tmp_path / "wrong-password", "wrong-password-123")
    with pytest.raises(ValueError, match="非法路径"):
        backup.unpack(encrypted, tmp_path / "unsafe", "test-password-1234")
    assert not (tmp_path / "escape.txt").exists()
    content = bytearray(encrypted.read_bytes()); content[-1] ^= 1; encrypted.write_bytes(content)
    with pytest.raises(ValueError, match="密码错误"):
        backup.unpack(encrypted, tmp_path / "damaged", "test-password-1234")


def test_full_snapshot_restores_to_new_database(client, tmp_path, monkeypatch):
    settings = get_settings()
    (settings.uploads_root / "backup-fixture.bin").write_bytes(b"media\x00\xff")
    (settings.site_backup_runtime_root / "state.json").write_text('{"memory":"会话记忆"}', encoding="utf-8")

    async def run():
        # Include encrypted provider data so a new deployment key is exercised.
        async with source_engine.begin() as conn:
            provider = (await conn.execute(select(Provider.__table__).limit(1))).mappings().one()
            original_key = provider["encrypted_api_key"]
            await conn.execute(Provider.__table__.update().where(Provider.id == provider["id"]).values(
                encrypted_api_key=SecretBox().encrypt("backup-test-provider-key")))
        job = backup.new_job("backup", "test")
        stage = backup.job_dir(job["id"]) / "test-snapshot"
        manifest = await backup.snapshot(stage, job["id"])
        archive = tmp_path / "site.cfbackup"
        await asyncio.to_thread(backup.pack, stage, archive, "test-password-1234")
        incoming = tmp_path / "validated"
        checked = await asyncio.to_thread(backup.unpack, archive, incoming, "test-password-1234")
        assert checked["counts"] == manifest["counts"]
        assert (incoming / "files/uploads/backup-fixture.bin").read_bytes() == b"media\x00\xff"
        assert "会话记忆" in (incoming / "files/runtime/state.json").read_text(encoding="utf-8")
        target = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
        @event.listens_for(target.sync_engine, "connect")
        def fk(dbapi, _): dbapi.execute("PRAGMA foreign_keys=ON")
        try:
            async with target.begin() as conn: await conn.run_sync(Base.metadata.create_all)
            monkeypatch.setattr(settings, "credential_encryption_secret", "different-server-key")
            async with target.begin() as conn: await backup.restore_database(conn, incoming, checked)
            async with target.connect() as conn:
                for name, count in manifest["counts"].items():
                    assert await conn.scalar(select(func.count()).select_from(Base.metadata.tables[name])) == count
                restored = await conn.scalar(select(Provider.encrypted_api_key).where(Provider.id == provider["id"]))
                assert SecretBox().decrypt(restored) == "backup-test-provider-key"
                assert (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
            # A second restore replaces the database rather than duplicating rows.
            async with target.begin() as conn: await backup.restore_database(conn, incoming, checked)
            restored_roots = {name: tmp_path / "restored-files" / name for name in backup.data_roots()}
            for root in restored_roots.values(): root.mkdir(parents=True)
            obsolete = restored_roots["uploads"] / "obsolete.txt"
            obsolete.write_text("not present in snapshot")
            monkeypatch.setattr(backup, "data_roots", lambda: restored_roots)
            await backup.install_files(incoming)
            assert not obsolete.exists()
            assert (restored_roots["uploads"] / "backup-fixture.bin").read_bytes() == b"media\x00\xff"
            assert "会话记忆" in (restored_roots["runtime"] / "state.json").read_text(encoding="utf-8")
        finally:
            await target.dispose()
            async with source_engine.begin() as conn:
                await conn.execute(Provider.__table__.update().where(Provider.id == provider["id"]).values(encrypted_api_key=original_key))
    asyncio.run(run())


def test_maintenance_blocks_new_work_and_releases(client):
    async def run():
        await gate.enter("test-maintenance")
        try:
            async with gate.activity() as allowed: assert not allowed
        finally: gate.leave("test-maintenance")
        async with gate.activity() as allowed: assert allowed
    asyncio.run(run())
    assert not gate.locked()


def test_backup_routes_restrict_users_and_recheck_password(client, creator_headers, admin_headers):
    for suffix in ("", "/"):
        response = client.get("/api/v1/admin/site-backups" + suffix,
                              headers={**admin_headers, "Host": "192.168.88.110:8580"}, follow_redirects=False)
        assert response.status_code == 200
        assert "location" not in response.headers
    assert client.get("/api/v1/admin/site-backups", headers=creator_headers).status_code == 403
    assert client.get("/api/v1/admin/site-backups", headers=admin_headers).status_code == 200
    assert client.post("/api/v1/admin/site-backups", headers=admin_headers, json={
        "admin_password": "incorrect", "backup_password": "test-password-1234",
    }).status_code == 403
    assert client.get("/api/v1/admin/site-backups/not-a-path/download?ticket=bad").status_code == 403


def test_backup_maintenance_middleware(client, admin_headers):
    asyncio.run(gate.enter("test-http"))
    try:
        assert client.get("/api/v1/projects", headers=admin_headers).status_code == 503
        assert client.get("/api/v1/projects", headers={**admin_headers, "Accept": "text/event-stream"}).status_code == 503
        assert client.get("/api/v1/admin/site-backups", headers=admin_headers).status_code == 200
        assert client.get("/health").status_code == 200
    finally: gate.leave("test-http")


def test_crashed_lease_does_not_block_maintenance(client):
    stale = gate.root() / "lease-crashed-process"
    stale.write_bytes(b"1")
    asyncio.run(gate.enter("test-stale"))
    try:
        assert not stale.exists()
    finally:
        gate.leave("test-stale")


def test_failed_restore_uses_safety_snapshot(client, monkeypatch):
    applied = []
    manifest = {"created_at": "2026-09-13", "database": "sqlite", "counts": {}, "files": {}}
    def unpack(source, destination, password):
        destination.mkdir()
        return manifest
    async def snapshot(destination, job_id):
        destination.mkdir()
        (destination / "manifest.json").write_text(json.dumps(manifest))
        return manifest
    async def apply(folder, _manifest):
        applied.append(folder.name)
        if folder.name == "incoming": raise OSError("simulated storage failure")
    monkeypatch.setattr(backup, "unpack", unpack)
    monkeypatch.setattr(backup, "snapshot", snapshot)
    monkeypatch.setattr(backup, "pack", lambda folder, target, password: target.write_bytes(b"test-only"))
    monkeypatch.setattr(backup, "apply_snapshot", apply)
    job = backup.new_job("restore", "test")
    asyncio.run(backup.restore_backup(job["id"], job["id"], "test-password-1234"))
    assert applied == ["incoming", "rollback"]
    assert backup.state(job["id"])["phase"] == "rolled_back"
    assert not gate.locked()
