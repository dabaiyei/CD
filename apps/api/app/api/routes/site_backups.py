from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.config import get_settings
from app.core.security import verify_password
from app.db.models import User, UserRole
from app.db.session import get_session
from app.services import site_backup as backups
from app.services import site_maintenance as maintenance

router = APIRouter(prefix="/admin/site-backups", tags=["site-backups"])


async def site_admin(user: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    emails = {e.casefold() for e in get_settings().site_backup_admin_emails}
    if emails:
        allowed = user.email.casefold() in emails
    else:
        # Existing tenant admins are not silently elevated to cross-tenant
        # access. The first site administrator is the bootstrap owner.
        owner = await session.scalar(select(User.id).where(User.role == UserRole.ADMIN).order_by(User.created_at, User.id).limit(1))
        allowed = owner == user.id
    if not allowed: raise HTTPException(403, "仅站点管理员可备份或覆盖全站；请由部署者配置 SITE_BACKUP_ADMIN_EMAILS")
    return user


class PasswordAction(BaseModel):
    admin_password: SecretStr
    backup_password: SecretStr = Field(min_length=12, max_length=256)


class RestoreAction(PasswordAction):
    fingerprint: str
    confirmation: str


_failed_passwords: dict[str, list[float]] = {}


async def reauthenticate(user, password):
    now = time.monotonic()
    attempts = [t for t in _failed_passwords.get(user.id, []) if now-t < 300]
    _failed_passwords[user.id] = attempts
    if len(attempts) >= 5: raise HTTPException(429, "密码校验失败过多，请五分钟后再试")
    if not await asyncio.to_thread(verify_password, password.get_secret_value(), user.password_hash):
        attempts.append(now)
        raise HTTPException(403, "当前管理员密码不正确")
    _failed_passwords.pop(user.id, None)


def get_job(job_id):
    try: return backups.state(job_id)
    except (ValueError, FileNotFoundError): raise HTTPException(404, "备份记录不存在") from None


@router.get("")
@router.get("/", include_in_schema=False)
async def listing(user=Depends(site_admin)):
    return {"items": await asyncio.to_thread(backups.list_jobs), "maintenance": maintenance.locked(),
            "max_bytes": get_settings().site_backup_max_bytes}


@router.post("", status_code=202)
@router.post("/", status_code=202, include_in_schema=False)
async def create(payload: PasswordAction, user=Depends(site_admin)):
    await reauthenticate(user, payload.admin_password)
    if maintenance.locked(): raise HTTPException(409, "站点正处于维护操作中")
    job = backups.new_job("backup", user.id)
    backups.launch(backups.create_backup(job["id"], payload.backup_password.get_secret_value()))
    return job


@router.post("/upload", status_code=201)
async def upload(request: Request, user=Depends(site_admin)):
    if maintenance.locked(): raise HTTPException(409, "请等待维护操作结束")
    maximum = get_settings().site_backup_max_bytes
    if int(request.headers.get("content-length") or 0) > maximum: raise HTTPException(413, "备份文件超过容量上限")
    job = backups.new_job("upload", user.id, status="uploading", message="正在接收备份文件")
    path = backups.job_dir(job["id"]) / "site.cfbackup"
    size = 0
    try:
        with path.open("wb") as handle:
            async for block in request.stream():
                size += len(block)
                if size > maximum: raise HTTPException(413, "备份文件超过容量上限")
                await asyncio.to_thread(handle.write, block)
        if size < 52 or await asyncio.to_thread(lambda: path.open("rb").read(8)) != backups.MAGIC:
            raise HTTPException(422, "不是本站支持的加密备份格式")
        return backups.save_state(job["id"], status="uploaded", bytes=size, message="上传完成，请校验备份")
    except BaseException:
        path.unlink(missing_ok=True)
        backups.save_state(job["id"], status="failed", message="上传中断或文件无效")
        raise


@router.post("/{job_id}/validate", status_code=202)
async def validate(job_id: str, payload: PasswordAction, user=Depends(site_admin)):
    await reauthenticate(user, payload.admin_password)
    if maintenance.locked(): raise HTTPException(409, "请等待维护操作结束")
    job = get_job(job_id)
    if job["status"] not in {"uploaded", "invalid", "ready", "completed", "failed"}:
        raise HTTPException(409, "当前记录正在处理中")
    if not (backups.job_dir(job_id)/"site.cfbackup").is_file(): raise HTTPException(404, "备份文件不存在")
    backups.save_state(job_id, status="validating", progress=0)
    backups.launch(backups.validate_backup(job_id, payload.backup_password.get_secret_value()))
    return get_job(job_id)


@router.post("/{job_id}/restore", status_code=202)
async def restore(job_id: str, payload: RestoreAction, user=Depends(site_admin)):
    await reauthenticate(user, payload.admin_password)
    source = get_job(job_id)
    if source["status"] != "ready" or source.get("fingerprint") != payload.fingerprint:
        raise HTTPException(409, "请先校验备份并确认该备份的预览")
    if payload.confirmation != "还原整个站点": raise HTTPException(422, "请输入：还原整个站点")
    if maintenance.locked(): raise HTTPException(409, "站点正在备份或还原")
    job = backups.new_job("restore", user.id, source_id=job_id)
    backups.launch(backups.restore_backup(job["id"], job_id, payload.backup_password.get_secret_value()))
    return job


@router.post("/{job_id}/download-ticket")
async def ticket(job_id: str, user=Depends(site_admin)):
    job = get_job(job_id)
    if not job.get("downloadable"): raise HTTPException(409, "备份尚未生成")
    token = jwt.encode({"backup_id": job_id, "purpose": "backup-download",
                        "exp": datetime.now(timezone.utc)+timedelta(minutes=2)}, get_settings().jwt_secret, algorithm="HS256")
    return {"url": f"{get_settings().api_prefix}/admin/site-backups/{job_id}/download?ticket={token}"}


@router.get("/{job_id}/download")
async def download(job_id: str, ticket: str):
    try:
        claims = jwt.decode(ticket, get_settings().jwt_secret, algorithms=["HS256"])
        if claims.get("purpose") != "backup-download" or claims.get("backup_id") != job_id: raise ValueError()
    except (jwt.InvalidTokenError, ValueError): raise HTTPException(403, "下载链接无效或已过期") from None
    get_job(job_id)
    path = backups.job_dir(job_id)/"site.cfbackup"
    if not path.is_file(): raise HTTPException(404, "备份文件不存在")
    return FileResponse(path, filename=f"cineforge-{job_id}.cfbackup", media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.delete("/{job_id}", status_code=204)
async def remove(job_id: str, user=Depends(site_admin)):
    job = get_job(job_id)
    if maintenance.locked() or job["status"] in {"queued", "running", "validating", "uploading", "recovery_required"}:
        raise HTTPException(409, "正在处理的备份或恢复点不能删除")
    import shutil
    await asyncio.to_thread(shutil.rmtree, backups.job_dir(job_id))


@router.post("/recovery/retry", status_code=202)
async def retry_recovery(payload: PasswordAction, user=Depends(site_admin)):
    await reauthenticate(user, payload.admin_password)
    if backups.JOBS: raise HTTPException(409, "请等待当前备份操作结束")
    if not any(j["status"] == "recovery_required" for j in backups.list_jobs()): raise HTTPException(409, "没有待回滚的还原任务")
    backups.launch(backups.recover_interrupted())
    return {"message": "正在恢复到还原前状态"}
