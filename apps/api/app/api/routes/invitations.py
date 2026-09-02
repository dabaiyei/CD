from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_admin
from app.api.routes.auth import client_ip, issue_session, set_refresh_cookie
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import (
    CreditAccount,
    CreditLedger,
    InvitationCode,
    InvitationRedemption,
    SecurityEvent,
    Tenant,
    User,
    UserRole,
    new_id,
)
from app.db.session import get_session
from app.domain.schemas import (
    InvitationCreate,
    InvitationPublic,
    InvitationRegistrationInfo,
    InvitationSettingsPublic,
    InvitationSettingsUpdate,
    InvitationUpdate,
    TokenResponse,
)
from app.services.auth_security import private_fingerprint
from app.services.media import ALLOWED_COVER_TYPES, MAX_COVER_BYTES, InvalidCoverImage, save_user_avatar
from app.services.object_storage import delete_media_file, persist_media_file

router = APIRouter(tags=["invitations"])
logger = logging.getLogger(__name__)


def normalized_url_prefix(value: str) -> str:
    prefix = value.strip().rstrip("/")
    parsed = urlsplit(prefix)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="邀请链接前缀必须是有效的 HTTP 或 HTTPS 网址")
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=422, detail="邀请链接前缀不能包含查询参数或片段")
    return prefix


def invitation_url(invitation: InvitationCode, prefix: str | None) -> str | None:
    return f"{prefix.rstrip('/')}/invite/{invitation.code}" if prefix else None


def invitation_public(invitation: InvitationCode, prefix: str | None) -> InvitationPublic:
    return InvitationPublic(
        id=invitation.id,
        code=invitation.code,
        name=invitation.name,
        max_registrations=invitation.max_registrations,
        registration_count=invitation.registration_count,
        remaining_registrations=max(0, invitation.max_registrations - invitation.registration_count),
        initial_credits=invitation.initial_credits,
        enabled=invitation.enabled,
        invite_url=invitation_url(invitation, prefix),
        created_at=invitation.created_at,
        updated_at=invitation.updated_at,
    )


def record_invitation_event(
    session: AsyncSession,
    *,
    request: Request,
    invitation_id: str,
    event_type: str,
    tenant_id: str,
    user_id: str,
    metadata: dict | None = None,
) -> None:
    session.add(
        SecurityEvent(
            tenant_id=tenant_id,
            user_id=user_id,
            event_type=event_type,
            success=True,
            subject_hash=hashlib.sha256(f"invitation:{invitation_id}".encode()).hexdigest(),
            ip_hash=private_fingerprint(client_ip(request)),
            user_agent=request.headers.get("user-agent", "")[:500],
            event_metadata={"invitation_id": invitation_id, **(metadata or {})},
        )
    )


async def active_invitation(session: AsyncSession, code: str) -> tuple[InvitationCode, Tenant]:
    row = (
        await session.execute(
            select(InvitationCode, Tenant)
            .join(Tenant, Tenant.id == InvitationCode.tenant_id)
            .where(InvitationCode.code == code, InvitationCode.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="邀请链接不存在或已失效")
    invitation, tenant = row
    if not tenant.is_active:
        raise HTTPException(status_code=410, detail="邀请所属空间当前不可用")
    if not invitation.enabled:
        raise HTTPException(status_code=410, detail="该邀请码已停用")
    if invitation.registration_count >= invitation.max_registrations:
        raise HTTPException(status_code=410, detail="该邀请码的注册名额已用完")
    return invitation, tenant


async def unique_invitation_code(session: AsyncSession) -> str:
    for _ in range(8):
        code = secrets.token_urlsafe(18).replace("-", "").replace("_", "")
        if not await session.scalar(select(InvitationCode.id).where(InvitationCode.code == code)):
            return code
    raise HTTPException(status_code=503, detail="邀请码生成失败，请稍后重试")


@router.get("/admin/invitations/settings", response_model=InvitationSettingsPublic)
async def get_invitation_settings(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> InvitationSettingsPublic:
    tenant = await session.get(Tenant, admin.tenant_id)
    return InvitationSettingsPublic(url_prefix=tenant.invite_url_prefix or "" if tenant else "")


@router.put("/admin/invitations/settings", response_model=InvitationSettingsPublic)
async def update_invitation_settings(
    payload: InvitationSettingsUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> InvitationSettingsPublic:
    tenant = await session.get(Tenant, admin.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    tenant.invite_url_prefix = normalized_url_prefix(payload.url_prefix)
    record_invitation_event(
        session,
        request=request,
        invitation_id=tenant.id,
        event_type="invitation_settings_updated",
        tenant_id=tenant.id,
        user_id=admin.id,
        metadata={"url_prefix": tenant.invite_url_prefix},
    )
    await session.commit()
    return InvitationSettingsPublic(url_prefix=tenant.invite_url_prefix)


@router.get("/admin/invitations", response_model=list[InvitationPublic])
async def list_invitations(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[InvitationPublic]:
    tenant = await session.get(Tenant, admin.tenant_id)
    invitations = list(
        (
            await session.scalars(
                select(InvitationCode)
                .where(InvitationCode.tenant_id == admin.tenant_id, InvitationCode.deleted_at.is_(None))
                .order_by(InvitationCode.created_at.desc(), InvitationCode.id.desc())
            )
        ).all()
    )
    prefix = tenant.invite_url_prefix if tenant else None
    return [invitation_public(item, prefix) for item in invitations]


@router.post("/admin/invitations", response_model=InvitationPublic, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    payload: InvitationCreate,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> InvitationPublic:
    invitation = InvitationCode(
        tenant_id=admin.tenant_id,
        created_by_id=admin.id,
        code=await unique_invitation_code(session),
        name=payload.name,
        max_registrations=payload.max_registrations,
        initial_credits=payload.initial_credits,
        enabled=payload.enabled,
    )
    session.add(invitation)
    await session.flush()
    record_invitation_event(
        session,
        request=request,
        invitation_id=invitation.id,
        event_type="invitation_created",
        tenant_id=admin.tenant_id,
        user_id=admin.id,
        metadata={
            "max_registrations": payload.max_registrations,
            "initial_credits": str(payload.initial_credits),
        },
    )
    await session.commit()
    tenant = await session.get(Tenant, admin.tenant_id)
    return invitation_public(invitation, tenant.invite_url_prefix if tenant else None)


@router.patch("/admin/invitations/{invitation_id}", response_model=InvitationPublic)
async def update_invitation(
    invitation_id: str,
    payload: InvitationUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> InvitationPublic:
    invitation = await session.scalar(
        select(InvitationCode).where(
            InvitationCode.id == invitation_id,
            InvitationCode.tenant_id == admin.tenant_id,
            InvitationCode.deleted_at.is_(None),
        )
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    values = payload.model_dump(exclude_unset=True)
    if values.get("max_registrations", invitation.max_registrations) < invitation.registration_count:
        raise HTTPException(status_code=409, detail="注册人数上限不能低于已注册人数")
    for key, value in values.items():
        setattr(invitation, key, value)
    record_invitation_event(
        session,
        request=request,
        invitation_id=invitation.id,
        event_type="invitation_updated",
        tenant_id=admin.tenant_id,
        user_id=admin.id,
        metadata={key: str(value) for key, value in values.items()},
    )
    await session.commit()
    await session.refresh(invitation)
    tenant = await session.get(Tenant, admin.tenant_id)
    return invitation_public(invitation, tenant.invite_url_prefix if tenant else None)


@router.delete("/admin/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invitation(
    invitation_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    invitation = await session.scalar(
        select(InvitationCode).where(
            InvitationCode.id == invitation_id,
            InvitationCode.tenant_id == admin.tenant_id,
            InvitationCode.deleted_at.is_(None),
        )
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    invitation.enabled = False
    invitation.deleted_at = datetime.now(UTC)
    record_invitation_event(
        session,
        request=request,
        invitation_id=invitation.id,
        event_type="invitation_deleted",
        tenant_id=admin.tenant_id,
        user_id=admin.id,
    )
    await session.commit()


@router.get("/invitations/{code}", response_model=InvitationRegistrationInfo)
async def get_registration_invitation(
    code: str,
    session: AsyncSession = Depends(get_session),
) -> InvitationRegistrationInfo:
    invitation, tenant = await active_invitation(session, code)
    return InvitationRegistrationInfo(
        code=invitation.code,
        tenant_name=tenant.name,
        invitation_name=invitation.name,
        initial_credits=invitation.initial_credits,
        remaining_registrations=invitation.max_registrations - invitation.registration_count,
    )


@router.post("/invitations/{code}/register", response_model=TokenResponse)
async def register_with_invitation(
    code: str,
    request: Request,
    response: Response,
    email: Annotated[str, Form(min_length=3, max_length=255)],
    display_name: Annotated[str, Form(min_length=1, max_length=80)],
    password: Annotated[str, Form(min_length=8, max_length=128)],
    avatar: Annotated[UploadFile | None, File()] = None,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    invitation, tenant = await active_invitation(session, code)
    normalized_email = email.strip().lower()
    normalized_name = display_name.strip()
    if "@" not in normalized_email or normalized_email.startswith("@") or normalized_email.endswith("@"):
        raise HTTPException(status_code=422, detail="请输入有效邮箱")
    if not normalized_name:
        raise HTTPException(status_code=422, detail="请输入名称")
    if await session.scalar(
        select(User.id).where(User.tenant_id == tenant.id, User.email == normalized_email)
    ):
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    user_id = new_id()
    password_hash = await run_in_threadpool(hash_password, password)
    stored_path: Path | None = None
    storage_key: str | None = None
    avatar_url: str | None = None
    if avatar is not None:
        if avatar.content_type not in ALLOWED_COVER_TYPES:
            await avatar.close()
            raise HTTPException(status_code=415, detail="头像仅支持 JPG、PNG 或 WebP 图片")
        data = await avatar.read(MAX_COVER_BYTES + 1)
        await avatar.close()
        if len(data) > MAX_COVER_BYTES:
            raise HTTPException(status_code=413, detail="头像图片不能超过 8 MB")
        if not data:
            raise HTTPException(status_code=422, detail="上传的头像为空")
        try:
            _local_url, stored_path = await run_in_threadpool(
                save_user_avatar,
                data,
                uploads_root=get_settings().uploads_root,
                tenant_id=tenant.id,
                user_id=user_id,
            )
            storage_key, avatar_url = await persist_media_file(stored_path, "image/webp")
        except InvalidCoverImage as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception:
            if stored_path is not None:
                await run_in_threadpool(stored_path.unlink, missing_ok=True)
            raise

    try:
        claimed = await session.execute(
            update(InvitationCode)
            .where(
                InvitationCode.id == invitation.id,
                InvitationCode.enabled.is_(True),
                InvitationCode.deleted_at.is_(None),
                InvitationCode.registration_count < InvitationCode.max_registrations,
            )
            .values(registration_count=InvitationCode.registration_count + 1)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise HTTPException(status_code=410, detail="该邀请码的注册名额已用完或已停用")
        await session.refresh(invitation)

        user = User(
            id=user_id,
            tenant_id=tenant.id,
            email=normalized_email,
            display_name=normalized_name,
            password_hash=password_hash,
            role=UserRole.USER,
            is_active=True,
            avatar_url=avatar_url,
            avatar_storage_path=storage_key,
        )
        session.add(user)
        await session.flush()
        session.add(
            CreditAccount(
                tenant_id=tenant.id,
                user_id=user.id,
                balance=invitation.initial_credits,
            )
        )
        if invitation.initial_credits > 0:
            session.add(
                CreditLedger(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    amount=invitation.initial_credits,
                    balance_after=invitation.initial_credits,
                    reason="邀请码注册初始积分",
                    reference_type="invitation",
                    reference_id=invitation.id,
                )
            )
        session.add(
            InvitationRedemption(
                invitation_id=invitation.id,
                tenant_id=tenant.id,
                user_id=user.id,
                initial_credits=invitation.initial_credits,
            )
        )
        tokens, refresh_token, refresh_record = issue_session(user)
        session.add(refresh_record)
        record_invitation_event(
            session,
            request=request,
            invitation_id=invitation.id,
            event_type="invitation_registered",
            tenant_id=tenant.id,
            user_id=user.id,
            metadata={"initial_credits": str(invitation.initial_credits)},
        )
        await session.commit()
    except HTTPException:
        await session.rollback()
        if storage_key:
            await delete_media_file(storage_key, stored_path)
        raise
    except IntegrityError as exc:
        await session.rollback()
        if storage_key:
            await delete_media_file(storage_key, stored_path)
        raise HTTPException(status_code=409, detail="该邮箱已注册") from exc
    except Exception:
        await session.rollback()
        if storage_key:
            try:
                await delete_media_file(storage_key, stored_path)
            except Exception:
                logger.warning("Failed to clean invitation avatar %s", storage_key, exc_info=True)
        raise

    set_refresh_cookie(response, refresh_token)
    return tokens
