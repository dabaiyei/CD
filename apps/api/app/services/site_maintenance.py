"""Cross-process maintenance gate shared by API and Worker through one volume.

OS file locks distinguish slow requests from crashed processes. A restore crash
leaves the site locked until startup recovery/operation cleanup succeeds.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from starlette.responses import JSONResponse
from app.core.config import get_settings


def root():
    path = get_settings().site_backup_root / "control"
    path.mkdir(parents=True, exist_ok=True)
    return path


def locked() -> bool:
    return (root() / "maintenance").exists()


def lock_file(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def claim_coordinator():
    handle = (root() / "coordinator").open("a+b")
    try:
        if handle.seek(0, 2) == 0:
            handle.write(b"1"); handle.flush()
        lock_file(handle)
        return handle
    except OSError:
        handle.close()
        raise RuntimeError("全站备份协调目录已被其他 API 实例使用；当前部署请仅运行一个 API 进程") from None


def active_leases() -> bool:
    active = False
    for path in root().glob("lease-*"):
        try:
            with path.open("r+b") as handle:
                lock_file(handle)
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError:
            active = True
    return active


@asynccontextmanager
async def activity():
    path = root() / f"lease-{os.getpid()}-{uuid.uuid4().hex}"
    if locked():
        yield False
        return
    handle = path.open("x+b")
    handle.write(b"1"); handle.flush()
    try:
        lock_file(handle)
        # Close the race with a backup beginning between the check and touch.
        yield not locked()
    finally:
        handle.close()
        path.unlink(missing_ok=True)


async def enter(job_id: str):
    path = root() / "maintenance"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(job_id)
    except FileExistsError:
        raise ValueError("站点正在备份或还原，请等待当前操作完成") from None
    try:
        for _ in range(120):
            if not active_leases():
                return
            await asyncio.sleep(.5)
        raise ValueError("仍有请求或生成任务运行，请待其结束后重试（不会强制中断任务）")
    except BaseException:
        leave(job_id)
        raise


def leave(job_id: str):
    path = root() / "maintenance"
    if path.exists() and path.read_text(encoding="utf-8") == job_id:
        path.unlink()


class SiteMaintenanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path == "/health" or path.startswith(
            get_settings().api_prefix + "/admin/site-backups"
        ):
            return await self.app(scope, receive, send)
        # Event streams are read-only and can last indefinitely. All writes and
        # ordinary GET handlers (some reconcile persisted state) hold a lease.
        stream = scope.get("method") == "GET" and path == get_settings().api_prefix + "/notifications/stream"
        if stream and not locked():
            return await self.app(scope, receive, send)
        async with activity() as allowed:
            if not allowed:
                return await JSONResponse(
                    {"detail": "站点正在备份或还原，暂时暂停操作，请稍后重试"},
                    status_code=503, headers={"Retry-After": "10"},
                )(scope, receive, send)
            await self.app(scope, receive, send)
