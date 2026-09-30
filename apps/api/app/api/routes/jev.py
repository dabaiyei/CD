import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.security import SecretBox
from app.db.models import JevConfiguration, JevPlatformSettings, User
from app.db.session import get_session
from app.services.jev_configuration import JEV_PLATFORMS, get_jev_configuration
from app.services.personal_routing import evaluate

router = APIRouter(prefix="/admin/jev", tags=["admin"])


class JevUpdate(BaseModel):
    enabled: bool
    provider: Literal["typesafe", "opencode_zen"] | None = None
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    model: str | None = Field(default=None, pattern=r"^jev-[a-zA-Z0-9.\-]+$", max_length=120)
    timeout_seconds: float = Field(default=8, gt=0, le=30)
    route_confidence: float = Field(default=0.65, ge=0, le=1)


def public(config):
    return {
        "enabled": config.enabled,
        "has_api_key": bool(config.api_key),
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "route_confidence": config.route_confidence,
        "source": config.source,
        "provider": config.provider,
        "endpoint": config.endpoint,
    }


async def public_configuration(db, tenant_id):
    config = await get_jev_configuration(db, tenant_id)
    result = public(config)
    result["platforms"] = [
        {**info, **public(await get_jev_configuration(db, tenant_id, provider=provider))}
        for provider, info in JEV_PLATFORMS.items()
    ]
    return result


@router.get("")
async def read_configuration(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    return await public_configuration(db, admin.tenant_id)


@router.put("")
async def save_configuration(
    payload: JevUpdate, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_session)
):
    config = await get_jev_configuration(db, admin.tenant_id, provider=payload.provider)
    key = payload.api_key.get_secret_value().strip() if payload.api_key else ""
    if payload.clear_api_key and key:
        raise HTTPException(422, "清除和更换密钥不能同时进行")
    key = "" if payload.clear_api_key else key or config.api_key
    if payload.enabled and not key:
        raise HTTPException(422, "启用 JEV 前请先填写 API Key")
    row = await db.get(JevConfiguration, admin.tenant_id)
    if row is None:
        original = await get_jev_configuration(db, admin.tenant_id, provider="typesafe")
        row = JevConfiguration(
            tenant_id=admin.tenant_id,
            encrypted_api_key=SecretBox().encrypt(original.api_key) if original.api_key else None,
            model=original.model,
        )
        db.add(row)
    row.enabled = payload.enabled
    encrypted_key = SecretBox().encrypt(key) if key else None
    model = payload.model or config.model
    if config.provider == "typesafe":
        row.encrypted_api_key = encrypted_key
        row.model = model
    platforms = await db.get(JevPlatformSettings, admin.tenant_id)
    if platforms is None:
        platforms = JevPlatformSettings(tenant_id=admin.tenant_id, profiles={})
        db.add(platforms)
    platforms.provider = config.provider
    platforms.profiles = {
        **(platforms.profiles or {}),
        config.provider: {"encrypted_api_key": encrypted_key, "model": model},
    }
    row.timeout_seconds = payload.timeout_seconds
    row.route_confidence = payload.route_confidence
    await db.commit()
    return await public_configuration(db, admin.tenant_id)


@router.post("/test")
async def test_configuration(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    config = await get_jev_configuration(db, admin.tenant_id)
    if not config.api_key:
        raise HTTPException(422, "请先保存 API Key")
    await db.commit()
    started = time.monotonic()
    try:
        result = await evaluate(
            {
                "message": "只写一个故事文案，不要生成图片或视频",
                "history": [],
                "creation": {},
                "attachments": [],
                "current_attachment_ids": [],
                "models": [],
            },
            config=config,
            use_laya=False,
        )
        if result.get("answers", {}).get("output", {}).get("choice") != "text":
            raise HTTPException(502, "接口已响应，但意图测试未返回预期文字类型")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"JEV 接口返回 HTTP {exc.response.status_code}，请检查密钥和模型") from None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise HTTPException(502, "JEV 连接超时或返回格式异常，请检查网络与配置") from None
    return {
        "ok": True,
        "model": result.get("model", config.model),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "output": "text",
        "provider": config.provider,
    }
