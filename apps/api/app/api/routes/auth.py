from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import jwt
from fastapi import APIRouter, Cookie, Depends, File, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    token_hash,
    verify_password,
)
from app.db.models import (
    AuthLoginGuard,
    CreditAccount,
    RefreshSession,
    SecurityEvent,
    Tenant,
    User,
    new_id,
)
from app.db.session import get_session
from app.domain.schemas import LoginRequest, SessionPublic, TokenResponse, UserPublic
from app.services.auth_security import (
    consume_login_rate_limit,
    login_identity_hash,
    private_fingerprint,
)
from app.services.media import (
    ALLOWED_COVER_TYPES,
    MAX_COVER_BYTES,
    InvalidCoverImage,
    save_user_avatar,
)
from app.services.object_storage import delete_media_file, persist_media_file

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)
REFRESH_COOKIE = "cineforge_refresh"
DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$600000$8W77rt8B9-h7tRt-JN8eCg=="
    "$Al4zXOjXOqRFKTvnvVbE7b8Aj1SR5-XuiAUYQKKMRY4="
)


def unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="刷新会话无效或已过期")


def client_ip(request: Request) -> str:
    # Proxy headers are intentionally ignored unless trusted-proxy middleware is added at deployment.
    return request.client.host if request.client else "unknown"


def retry_later(seconds: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="登录尝试过多，请稍后再试",
        headers={"Retry-After": str(max(1, seconds))},
    )


def as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def avatar_cached_path(storage_path: str | None) -> Path | None:
    if not storage_path:
        return None
    root = get_settings().uploads_root.resolve()
    candidate = (root / storage_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def add_security_event(
    session: AsyncSession,
    *,
    request: Request,
    event_type: str,
    success: bool,
    subject_hash: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    session.add(
        SecurityEvent(
            tenant_id=tenant_id,
            user_id=user_id,
            event_type=event_type,
            success=success,
            subject_hash=subject_hash,
            ip_hash=private_fingerprint(client_ip(request)),
            user_agent=request.headers.get("user-agent", "")[:500],
            event_metadata=metadata or {},
        )
    )


async def record_failed_login(
    session: AsyncSession,
    *,
    identity_hash: str,
    tenant_id: str | None,
    user_id: str | None,
    now: datetime,
) -> datetime | None:
    settings = get_settings()
    guard = await session.scalar(
        select(AuthLoginGuard)
        .where(AuthLoginGuard.identity_hash == identity_hash)
        .with_for_update()
    )
    if guard is None:
        guard = AuthLoginGuard(
            identity_hash=identity_hash,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        session.add(guard)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            guard = await session.scalar(
                select(AuthLoginGuard)
                .where(AuthLoginGuard.identity_hash == identity_hash)
                .with_for_update()
            )
            if guard is None:
                raise

    last_failed_at = as_utc(guard.last_failed_at)
    reset_before = now - timedelta(minutes=settings.login_lockout_minutes)
    if last_failed_at is None or last_failed_at < reset_before:
        guard.failed_count = 0
        guard.locked_until = None
    guard.tenant_id = tenant_id
    guard.user_id = user_id
    guard.failed_count += 1
    guard.last_failed_at = now
    if guard.failed_count >= settings.login_max_failures:
        guard.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
    await session.flush()
    return as_utc(guard.locked_until)


async def revoke_active_sessions(
    session: AsyncSession,
    *,
    user_id: str,
    tenant_id: str,
    revoked_at: datetime,
) -> None:
    await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.user_id == user_id,
            RefreshSession.tenant_id == tenant_id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )


def set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="strict",
        path=f"{settings.api_prefix}/auth",
    )


def issue_session(user: User) -> tuple[TokenResponse, str, RefreshSession]:
    settings = get_settings()
    session_id = new_id()
    refresh_token, expires_at = create_refresh_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        session_id=session_id,
    )
    record = RefreshSession(
        id=session_id,
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=token_hash(refresh_token),
        expires_at=expires_at,
    )
    access_token = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role.value,
    )
    return (
        TokenResponse(access_token=access_token, expires_in=settings.access_token_minutes * 60),
        refresh_token,
        record,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    now = datetime.now(UTC)
    email = payload.email.strip().lower()
    requested_tenant = payload.tenant.strip().lower() if payload.tenant else None
    tenant: Tenant | None = None
    user: User | None = None

    if requested_tenant:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == requested_tenant))
        if tenant is not None and tenant.is_active:
            user = await session.scalar(
                select(User).where(User.tenant_id == tenant.id, User.email == email)
            )
    else:
        matches = (
            await session.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(User.email == email, Tenant.is_active.is_(True))
            )
        ).all()
        if len(matches) > 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该邮箱关联了多个租户，请联系管理员处理账号归属",
            )
        if matches:
            user, tenant = matches[0]

    tenant_slug = tenant.slug if tenant is not None else (requested_tenant or "auto")
    identity_hash = login_identity_hash(tenant_slug, email)
    if tenant is None or not tenant.is_active:
        user = None
    tenant_id = tenant.id if tenant else None
    user_id = user.id if user else None

    redis_retry = await consume_login_rate_limit(client_ip(request))
    if redis_retry is not None:
        add_security_event(
            session,
            request=request,
            event_type="login_rate_limited",
            success=False,
            subject_hash=identity_hash,
            tenant_id=tenant_id,
            user_id=user_id,
            metadata={"retry_after_seconds": redis_retry},
        )
        await session.commit()
        raise retry_later(redis_retry)

    guard = await session.get(AuthLoginGuard, identity_hash)
    locked_until = as_utc(guard.locked_until) if guard else None
    if locked_until is not None and locked_until > now:
        retry_after = max(1, int((locked_until - now).total_seconds()))
        add_security_event(
            session,
            request=request,
            event_type="login_locked",
            success=False,
            subject_hash=identity_hash,
            tenant_id=tenant_id,
            user_id=user_id,
            metadata={"retry_after_seconds": retry_after},
        )
        await session.commit()
        raise retry_later(retry_after)

    password_hash = user.password_hash if user is not None and user.is_active else DUMMY_PASSWORD_HASH
    password_valid = await run_in_threadpool(verify_password, payload.password, password_hash)
    if user is None or not user.is_active or not password_valid:
        locked_until = await record_failed_login(
            session,
            identity_hash=identity_hash,
            tenant_id=tenant_id,
            user_id=user_id,
            now=now,
        )
        add_security_event(
            session,
            request=request,
            event_type="login_failed",
            success=False,
            subject_hash=identity_hash,
            tenant_id=tenant_id,
            user_id=user_id,
            metadata={"locked": locked_until is not None},
        )
        await session.commit()
        if locked_until is not None:
            raise retry_later(int((locked_until - now).total_seconds()))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码不正确")

    if guard is not None:
        await session.delete(guard)
    add_security_event(
        session,
        request=request,
        event_type="login_succeeded",
        success=True,
        subject_hash=identity_hash,
        tenant_id=tenant.id,
        user_id=user.id,
    )
    tokens, refresh_token, refresh_session = issue_session(user)
    session.add(refresh_session)
    await session.commit()
    set_refresh_cookie(response, refresh_token)
    return tokens


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    if not refresh_token:
        raise unauthorized()
    try:
        payload = decode_refresh_token(refresh_token)
        session_id = str(payload["jti"])
        user_id = str(payload["sub"])
        tenant_id = str(payload["tenant_id"])
    except (jwt.InvalidTokenError, KeyError, TypeError) as error:
        raise unauthorized() from error
    refresh_session = await session.get(RefreshSession, session_id)
    now = datetime.now(UTC)
    if refresh_session is None or refresh_session.token_hash != token_hash(refresh_token):
        raise unauthorized()
    expires_at = refresh_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if refresh_session.revoked_at is not None or expires_at <= now:
        if refresh_session.revoked_at is not None:
            await revoke_active_sessions(
                session,
                user_id=refresh_session.user_id,
                tenant_id=refresh_session.tenant_id,
                revoked_at=now,
            )
            await session.commit()
        raise unauthorized()
    user = await session.get(User, user_id)
    if user is None or not user.is_active or user.tenant_id != tenant_id:
        raise unauthorized()

    claimed = await session.execute(
        update(RefreshSession)
        .where(RefreshSession.id == session_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    if claimed.rowcount != 1:
        await revoke_active_sessions(
            session,
            user_id=user_id,
            tenant_id=tenant_id,
            revoked_at=now,
        )
        await session.commit()
        raise unauthorized()

    tokens, rotated_token, rotated_session = issue_session(user)
    session.add(rotated_session)
    await session.flush()
    await session.execute(
        update(RefreshSession)
        .where(RefreshSession.id == session_id)
        .values(replaced_by_id=rotated_session.id)
    )
    await session.commit()
    set_refresh_cookie(response, rotated_token)
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> None:
    if refresh_token:
        try:
            payload = decode_refresh_token(refresh_token)
            refresh_session = await session.get(RefreshSession, str(payload["jti"]))
            if refresh_session and refresh_session.revoked_at is None:
                refresh_session.revoked_at = datetime.now(UTC)
                await session.commit()
        except (jwt.InvalidTokenError, KeyError, TypeError):
            pass
    response.delete_cookie(REFRESH_COOKIE, path=f"{get_settings().api_prefix}/auth")


@router.put("/me/avatar", response_model=UserPublic)
async def upload_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    if file.content_type not in ALLOWED_COVER_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 JPG、PNG 或 WebP 图片")

    data = await file.read(MAX_COVER_BYTES + 1)
    await file.close()
    if len(data) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="头像图片不能超过 8 MB")
    if not data:
        raise HTTPException(status_code=422, detail="上传的图片为空")

    settings = get_settings()
    try:
        _local_url, stored_path = await run_in_threadpool(
            save_user_avatar,
            data,
            uploads_root=settings.uploads_root,
            tenant_id=user.tenant_id,
            user_id=user.id,
        )
    except InvalidCoverImage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        storage_key, avatar_url = await persist_media_file(stored_path, "image/webp")
    except Exception:
        await run_in_threadpool(stored_path.unlink, missing_ok=True)
        raise

    previous_storage_path = user.avatar_storage_path
    user.avatar_url = avatar_url
    user.avatar_storage_path = storage_key
    try:
        await session.commit()
    except Exception:
        await delete_media_file(storage_key, stored_path)
        await session.rollback()
        raise
    await session.refresh(user)

    if previous_storage_path and previous_storage_path != storage_key:
        try:
            await delete_media_file(
                previous_storage_path,
                avatar_cached_path(previous_storage_path),
            )
        except Exception:
            logger.warning(
                "Failed to delete replaced avatar %s for user %s",
                previous_storage_path,
                user.id,
                exc_info=True,
            )
    return user


@router.delete("/me/avatar", response_model=UserPublic)
async def delete_avatar(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    previous_storage_path = user.avatar_storage_path
    user.avatar_url = None
    user.avatar_storage_path = None
    await session.commit()
    await session.refresh(user)

    if previous_storage_path:
        try:
            await delete_media_file(
                previous_storage_path,
                avatar_cached_path(previous_storage_path),
            )
        except Exception:
            logger.warning(
                "Failed to delete avatar %s for user %s",
                previous_storage_path,
                user.id,
                exc_info=True,
            )
    return user


@router.get("/me", response_model=SessionPublic)
async def me(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionPublic:
    tenant = await session.get(Tenant, user.tenant_id)
    account = await session.scalar(
        select(CreditAccount).where(
            CreditAccount.tenant_id == user.tenant_id, CreditAccount.user_id == user.id
        )
    )
    return SessionPublic(
        user=UserPublic.model_validate(user),
        tenant_name=tenant.name if tenant else "",
        tenant_slug=tenant.slug if tenant else "",
        credit_balance=account.balance if account else Decimal("0"),
    )
