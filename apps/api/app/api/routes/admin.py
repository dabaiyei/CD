from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_admin
from app.core.config import get_settings
from app.core.security import SecretBox, hash_password
from app.db.models import (
    AgentProfile,
    AIModel,
    AITask,
    AudioClip,
    CreditAccount,
    CreditLedger,
    Handbook,
    HandbookType,
    ImageResolutionModelRoute,
    ModelType,
    Notification,
    PricingRule,
    Project,
    PromptTemplate,
    Provider,
    ProviderType,
    RefreshSession,
    SecurityEvent,
    User,
    UserRole,
    VideoClip,
    VoiceBinding,
)
from app.db.session import get_session
from app.domain.schemas import (
    AdminCreditAdjustment,
    AdminCreditAdjustmentResult,
    AdminPasswordReset,
    AdminUserCreate,
    AdminUserPage,
    AdminUserPublic,
    AdminUserUpdate,
    AgentProfileCreate,
    AgentProfilePublic,
    ConnectivityResult,
    CreditLedgerPage,
    CreditLedgerPublic,
    HandbookCreate,
    HandbookManifestPublic,
    HandbookPackagePublic,
    HandbookPackageUpdate,
    HandbookPublic,
    HandbookUpdate,
    ImageResolutionModelRoutePublic,
    ImageResolutionModelRouteUpdate,
    ModelCreate,
    ModelDiscoveryResponse,
    ModelImportRequest,
    ModelImportResponse,
    ModelPublic,
    ModelUpdate,
    PricingRulePublic,
    PricingRuleUpdate,
    PromptTemplatePublic,
    PromptTemplateUpdate,
    ProviderCreate,
    ProviderPublic,
    ProviderUpdate,
    ReadinessPublic,
    SecurityEventPage,
    SecurityEventPublic,
)
from app.services.auth_security import private_fingerprint
from app.services.image_model_routing import IMAGE_RESOLUTIONS
from app.services.managed_skills import (
    SYSTEM_PROMPT_CODES,
    SYSTEM_PROMPTS,
    create_handbook_package,
    handbook_manifest,
    read_handbook_files,
    replace_handbook_files,
    write_prompt_file,
)
from app.services.media import (
    ALLOWED_COVER_TYPES,
    MAX_COVER_BYTES,
    InvalidCoverImage,
    save_handbook_cover,
)
from app.services.media_gateway import (
    ImageGenerationRequest,
    ModelGatewayError,
    OpenAICompatibleMediaGateway,
)
from app.services.object_storage import (
    delete_media_file,
    object_key_from_media_url,
    persist_media_file,
)
from app.services.provider_adapters import (
    AGNES_IMAGE_21_MODEL_ID,
    AGNES_IMAGE_MODEL_ID,
    AGNES_PROVIDER_CODE,
    AGNES_TEXT_MODEL_ID,
    AGNES_VIDEO_MODEL_ID,
    AUTODL_MINIMAX_H3_MODEL_ID,
    AUTODL_MINIMAX_H3_PROVIDER_CODE,
    ProviderAdapterConfig,
    agnes_image_21_capabilities,
    agnes_image_capabilities,
    agnes_video_adapter_config,
    agnes_video_capabilities,
    autodl_minimax_h3_adapter_config,
    autodl_minimax_h3_capabilities,
    normalize_video_capabilities,
)
from app.services.readiness import configured_image_resolutions, default_model_types

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)

MODEL_TYPE_HINTS: tuple[tuple[ModelType, tuple[str, ...]], ...] = (
    (ModelType.TTS, ("tts", "speech", "voice", "audio")),
    (ModelType.VIDEO, ("video", "sora", "kling", "veo", "runway", "wan2", "vidu")),
    (ModelType.IMAGE, ("image", "dall-e", "flux", "midjourney", "sdxl", "stable-diffusion")),
)
REQUIRED_DEFAULT_MODEL_TYPES = frozenset({ModelType.TEXT, ModelType.VIDEO})


def record_admin_user_event(
    session: AsyncSession,
    *,
    request: Request,
    admin: User,
    target: User,
    event_type: str,
    metadata: dict | None = None,
) -> None:
    session.add(
        SecurityEvent(
            tenant_id=admin.tenant_id,
            user_id=admin.id,
            event_type=event_type,
            success=True,
            subject_hash=hashlib.sha256(f"user:{target.id}".encode()).hexdigest(),
            ip_hash=private_fingerprint(request.client.host if request.client else "unknown"),
            user_agent=request.headers.get("user-agent", "")[:500],
            event_metadata={"target_user_id": target.id, **(metadata or {})},
        )
    )


def admin_users_query(tenant_id: str):
    project_stats = (
        select(
            Project.owner_id.label("user_id"),
            func.count(Project.id).label("project_count"),
        )
        .where(Project.tenant_id == tenant_id)
        .group_by(Project.owner_id)
        .subquery()
    )
    task_stats = (
        select(
            AITask.user_id.label("user_id"),
            func.count(AITask.id).label("task_count"),
            func.max(AITask.created_at).label("last_task_at"),
        )
        .where(AITask.tenant_id == tenant_id)
        .group_by(AITask.user_id)
        .subquery()
    )
    return (
        select(
            User,
            func.coalesce(CreditAccount.balance, 0).label("credit_balance"),
            func.coalesce(project_stats.c.project_count, 0).label("project_count"),
            func.coalesce(task_stats.c.task_count, 0).label("task_count"),
            task_stats.c.last_task_at,
        )
        .outerjoin(
            CreditAccount,
            and_(
                CreditAccount.tenant_id == User.tenant_id,
                CreditAccount.user_id == User.id,
            ),
        )
        .outerjoin(project_stats, project_stats.c.user_id == User.id)
        .outerjoin(task_stats, task_stats.c.user_id == User.id)
        .where(User.tenant_id == tenant_id)
    )


def admin_user_public(row) -> AdminUserPublic:
    user = row[0]
    return AdminUserPublic(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        credit_balance=row.credit_balance,
        project_count=int(row.project_count),
        task_count=int(row.task_count),
        last_task_at=row.last_task_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def admin_user_record(
    session: AsyncSession,
    user_id: str,
    tenant_id: str,
    *,
    for_update: bool = False,
) -> User:
    query = select(User).where(User.id == user_id, User.tenant_id == tenant_id)
    if for_update:
        query = query.with_for_update()
    user = await session.scalar(query)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


async def load_admin_user_public(
    session: AsyncSession,
    user_id: str,
    tenant_id: str,
) -> AdminUserPublic:
    row = (
        await session.execute(admin_users_query(tenant_id).where(User.id == user_id))
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return admin_user_public(row)


@router.get("/users", response_model=AdminUserPage)
async def list_users(
    search: str | None = Query(default=None, max_length=120),
    role: UserRole | None = None,
    is_active: bool | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserPage:
    filters = [User.tenant_id == admin.tenant_id]
    if search and search.strip():
        normalized = search.strip().lower()
        filters.append(
            or_(
                func.lower(User.email).contains(normalized, autoescape=True),
                func.lower(User.display_name).contains(normalized, autoescape=True),
            )
        )
    if role is not None:
        filters.append(User.role == role)
    if is_active is not None:
        filters.append(User.is_active.is_(is_active))

    total = int(await session.scalar(select(func.count(User.id)).where(*filters)) or 0)
    summary = (
        await session.execute(
            select(
                func.sum(case((User.is_active.is_(True), 1), else_=0)),
                func.sum(case((User.role == UserRole.ADMIN, 1), else_=0)),
                func.coalesce(func.sum(CreditAccount.balance), 0),
            )
            .outerjoin(
                CreditAccount,
                and_(
                    CreditAccount.tenant_id == User.tenant_id,
                    CreditAccount.user_id == User.id,
                ),
            )
            .where(User.tenant_id == admin.tenant_id)
        )
    ).one()
    rows = (
        await session.execute(
            admin_users_query(admin.tenant_id)
            .where(*filters)
            .order_by(User.created_at.desc(), User.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return AdminUserPage(
        items=[admin_user_public(row) for row in rows],
        total=total,
        active_count=int(summary[0] or 0),
        admin_count=int(summary[1] or 0),
        total_balance=summary[2],
    )


@router.post("/users", response_model=AdminUserPublic, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserPublic:
    password_hash = await run_in_threadpool(hash_password, payload.password)
    user = User(
        tenant_id=admin.tenant_id,
        email=payload.email,
        display_name=payload.display_name,
        password_hash=password_hash,
        role=payload.role,
        is_active=True,
    )
    session.add(user)
    try:
        await session.flush()
        account = CreditAccount(
            tenant_id=admin.tenant_id,
            user_id=user.id,
            balance=payload.initial_credits,
        )
        session.add(account)
        if payload.initial_credits > 0:
            session.add(
                CreditLedger(
                    tenant_id=admin.tenant_id,
                    user_id=user.id,
                    amount=payload.initial_credits,
                    balance_after=payload.initial_credits,
                    reason="管理员开户发放积分",
                    reference_type="admin_adjustment",
                    reference_id=admin.id,
                )
            )
        record_admin_user_event(
            session,
            request=request,
            admin=admin,
            target=user,
            event_type="admin_user_created",
            metadata={"role": user.role.value, "initial_credits": str(payload.initial_credits)},
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail="该邮箱已存在") from error
    return await load_admin_user_public(session, user.id, admin.tenant_id)


@router.patch("/users/{user_id}", response_model=AdminUserPublic)
async def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserPublic:
    user = await admin_user_record(session, user_id, admin.tenant_id, for_update=True)
    values = payload.model_dump(exclude_unset=True)
    next_role = values.get("role", user.role)
    next_active = values.get("is_active", user.is_active)
    if user.id == admin.id and (next_role != UserRole.ADMIN or not next_active):
        raise HTTPException(status_code=409, detail="不能停用当前账号或移除自己的管理员权限")
    if user.role == UserRole.ADMIN and user.is_active and (
        next_role != UserRole.ADMIN or not next_active
    ):
        other_admins = await session.scalar(
            select(func.count(User.id)).where(
                User.tenant_id == admin.tenant_id,
                User.id != user.id,
                User.role == UserRole.ADMIN,
                User.is_active.is_(True),
            )
        )
        if not other_admins:
            raise HTTPException(status_code=409, detail="租户必须至少保留一个有效管理员")

    revoke_sessions = next_role != user.role or next_active != user.is_active
    for field, value in values.items():
        setattr(user, field, value)
    if revoke_sessions:
        await session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.tenant_id == admin.tenant_id,
                RefreshSession.user_id == user.id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
    record_admin_user_event(
        session,
        request=request,
        admin=admin,
        target=user,
        event_type="admin_user_updated",
        metadata={"changed_fields": sorted(values)},
    )
    await session.commit()
    return await load_admin_user_public(session, user.id, admin.tenant_id)


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_password(
    user_id: str,
    payload: AdminPasswordReset,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    user = await admin_user_record(session, user_id, admin.tenant_id, for_update=True)
    user.password_hash = await run_in_threadpool(hash_password, payload.password)
    await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.tenant_id == admin.tenant_id,
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    record_admin_user_event(
        session,
        request=request,
        admin=admin,
        target=user,
        event_type="admin_user_password_reset",
    )
    await session.commit()


@router.post("/users/{user_id}/credits/adjust", response_model=AdminCreditAdjustmentResult)
async def adjust_user_credits(
    user_id: str,
    payload: AdminCreditAdjustment,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminCreditAdjustmentResult:
    user = await admin_user_record(session, user_id, admin.tenant_id)
    account = await session.scalar(
        select(CreditAccount)
        .where(
            CreditAccount.tenant_id == admin.tenant_id,
            CreditAccount.user_id == user.id,
        )
        .with_for_update()
    )
    if account is None:
        account = CreditAccount(tenant_id=admin.tenant_id, user_id=user.id, balance=0)
        session.add(account)
        await session.flush()
    next_balance = account.balance + payload.amount
    if next_balance < 0:
        raise HTTPException(status_code=409, detail="扣减后积分不能小于 0")
    account.balance = next_balance
    ledger = CreditLedger(
        tenant_id=admin.tenant_id,
        user_id=user.id,
        amount=payload.amount,
        balance_after=next_balance,
        reason=payload.reason,
        reference_type="admin_adjustment",
        reference_id=admin.id,
    )
    session.add(ledger)
    await session.flush()
    session.add(
        Notification(
            tenant_id=admin.tenant_id,
            user_id=user.id,
            category="billing",
            title="积分已发放" if payload.amount > 0 else "积分已扣减",
            message=f"{payload.reason}，变动 {payload.amount:+.2f}，当前余额 {next_balance:.2f}",
            notification_metadata={
                "amount": str(payload.amount),
                "balance_after": str(next_balance),
                "ledger_id": ledger.id,
            },
        )
    )
    record_admin_user_event(
        session,
        request=request,
        admin=admin,
        target=user,
        event_type="admin_credit_adjusted",
        metadata={
            "amount": str(payload.amount),
            "balance_after": str(next_balance),
            "ledger_id": ledger.id,
        },
    )
    await session.commit()
    await session.refresh(ledger)
    return AdminCreditAdjustmentResult(
        user=await load_admin_user_public(session, user.id, admin.tenant_id),
        ledger=CreditLedgerPublic.model_validate(ledger),
    )


@router.get("/users/{user_id}/credit-ledger", response_model=CreditLedgerPage)
async def list_user_credit_ledger(
    user_id: str,
    before: datetime | None = None,
    before_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=30, ge=1, le=100),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> CreditLedgerPage:
    await admin_user_record(session, user_id, admin.tenant_id)
    query = select(CreditLedger).where(
        CreditLedger.tenant_id == admin.tenant_id,
        CreditLedger.user_id == user_id,
    )
    if before is not None and before_id is not None:
        query = query.where(
            or_(
                CreditLedger.created_at < before,
                and_(CreditLedger.created_at == before, CreditLedger.id < before_id),
            )
        )
    elif before is not None:
        query = query.where(CreditLedger.created_at < before)
    entries = list(
        (
            await session.scalars(
                query.order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc()).limit(limit)
            )
        ).all()
    )
    return CreditLedgerPage(
        items=[CreditLedgerPublic.model_validate(entry) for entry in entries],
        next_before=entries[-1].created_at if len(entries) == limit else None,
        next_before_id=entries[-1].id if len(entries) == limit else None,
    )


@router.get("/security-events", response_model=SecurityEventPage)
async def list_security_events(
    before: datetime | None = None,
    before_id: str | None = Query(default=None, max_length=36),
    event_type: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
    success: bool | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SecurityEventPage:
    query = select(SecurityEvent).where(SecurityEvent.tenant_id == admin.tenant_id)
    if event_type is not None:
        query = query.where(SecurityEvent.event_type == event_type)
    if success is not None:
        query = query.where(SecurityEvent.success.is_(success))
    if before is not None and before_id is not None:
        query = query.where(
            or_(
                SecurityEvent.created_at < before,
                and_(SecurityEvent.created_at == before, SecurityEvent.id < before_id),
            )
        )
    elif before is not None:
        query = query.where(SecurityEvent.created_at < before)
    events = list(
        (
            await session.scalars(
                query.order_by(SecurityEvent.created_at.desc(), SecurityEvent.id.desc()).limit(limit)
            )
        ).all()
    )
    return SecurityEventPage(
        items=[SecurityEventPublic.model_validate(item) for item in events],
        next_before=events[-1].created_at if len(events) == limit else None,
        next_before_id=events[-1].id if len(events) == limit else None,
    )


def provider_public(provider: Provider) -> ProviderPublic:
    credentials = provider_credentials(provider)
    return ProviderPublic(
        id=provider.id,
        code=provider.code,
        name=provider.name,
        provider_type=provider.provider_type,
        base_url=provider.base_url,
        extra_headers=provider.extra_headers,
        adapter_config=provider.adapter_config,
        configured_credentials=sorted(credentials),
        max_concurrency=provider.max_concurrency,
        enabled=provider.enabled,
        has_api_key=bool(provider.encrypted_api_key),
        last_tested_at=provider.last_tested_at,
        last_test_ok=provider.last_test_ok,
        last_test_message=provider.last_test_message,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


async def tenant_record(session: AsyncSession, entity: type, record_id: str, tenant_id: str):
    record = await session.get(entity, record_id)
    if record is None or record.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="配置不存在")
    return record


async def active_model_reference_labels(
    session: AsyncSession,
    model: AIModel,
    tenant_id: str,
) -> list[str]:
    references = {
        "图片分辨率路由": await session.scalar(
            select(ImageResolutionModelRoute.id)
            .where(
                ImageResolutionModelRoute.tenant_id == tenant_id,
                ImageResolutionModelRoute.model_id == model.id,
            )
            .limit(1)
        ),
        "项目": await session.scalar(
            select(Project.id)
            .where(
                Project.tenant_id == tenant_id,
                (Project.video_model_id == model.id) | (Project.image_model_id == model.id),
            )
            .limit(1)
        ),
        "Agent": await session.scalar(
            select(AgentProfile.id)
            .where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.text_model_id == model.id,
                AgentProfile.enabled.is_(True),
            )
            .limit(1)
        ),
        "音色绑定": await session.scalar(
            select(VoiceBinding.id)
            .where(
                VoiceBinding.tenant_id == tenant_id,
                VoiceBinding.tts_model_id == model.id,
                VoiceBinding.enabled.is_(True),
            )
            .limit(1)
        ),
    }
    return [label for label, reference in references.items() if reference]


def provider_headers(provider: Provider) -> dict[str, str]:
    headers = dict(provider.extra_headers)
    key = SecretBox().decrypt(provider.encrypted_api_key)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def provider_credentials(provider: Provider) -> dict[str, str]:
    encrypted = SecretBox().decrypt(provider.encrypted_credentials)
    if not encrypted:
        return {}
    try:
        decoded = json.loads(encrypted)
    except json.JSONDecodeError:
        logger.warning("provider %s has invalid encrypted credentials payload", provider.id)
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): str(value) for key, value in decoded.items() if str(value)}


def normalize_provider_base_url(provider_code: str, value: str) -> str:
    normalized = value.rstrip("/")
    if provider_code == AUTODL_MINIMAX_H3_PROVIDER_CODE and normalized.lower().endswith("/api/v1"):
        return normalized[:-7].rstrip("/")
    return normalized


def normalize_provider_credential_values(
    provider_code: str,
    credentials: dict[str, str],
) -> dict[str, str]:
    normalized = dict(credentials)
    if provider_code == AUTODL_MINIMAX_H3_PROVIDER_CODE:
        token = normalized.get("apiKey", "").strip()
        if token.lower().startswith("bearer "):
            normalized["apiKey"] = token[7:].strip()
    return normalized


def validate_provider_credentials(
    adapter_config: dict,
    credentials: dict[str, str],
    *,
    require_required: bool = True,
) -> dict[str, str]:
    adapter = ProviderAdapterConfig.model_validate(adapter_config) if adapter_config else None
    if adapter is None:
        if credentials:
            raise HTTPException(status_code=422, detail="请先配置凭据字段，再填写自定义凭据")
        return {}
    fields = {item.key: item for item in adapter.credential_fields}
    unknown = sorted(set(credentials) - set(fields))
    if unknown:
        raise HTTPException(status_code=422, detail=f"存在未声明的凭据字段：{'、'.join(unknown)}")
    cleaned = {key: value.strip() for key, value in credentials.items() if value.strip()}
    oversized = [key for key, value in cleaned.items() if len(value) > 4096]
    if oversized:
        raise HTTPException(status_code=422, detail=f"凭据内容过长：{'、'.join(oversized)}")
    missing = [
        item.label
        for item in fields.values()
        if require_required and item.required and not cleaned.get(item.key)
    ]
    if missing:
        raise HTTPException(status_code=422, detail=f"缺少必填凭据：{'、'.join(missing)}")
    return cleaned


def provider_gateway(provider: Provider) -> OpenAICompatibleMediaGateway:
    return OpenAICompatibleMediaGateway(
        base_url=provider.base_url,
        api_key=SecretBox().decrypt(provider.encrypted_api_key),
        extra_headers=provider.extra_headers,
        adapter_config=provider.adapter_config,
        credentials=provider_credentials(provider),
    )


def infer_model_type(model_id: str) -> ModelType:
    normalized = model_id.lower()
    for model_type, hints in MODEL_TYPE_HINTS:
        if any(hint in normalized for hint in hints):
            return model_type
    return ModelType.TEXT


async def fetch_provider_models(provider: Provider) -> list[dict]:
    adapter = (
        ProviderAdapterConfig.model_validate(provider.adapter_config)
        if provider.adapter_config
        else None
    )
    if adapter and adapter.catalog:
        return await provider_gateway(provider).fetch_adapter_catalog()
    async with httpx.AsyncClient(timeout=get_settings().provider_test_timeout_seconds) as client:
        response = await client.get(
            urljoin(f"{provider.base_url}/", "models"),
            headers=provider_headers(provider),
        )
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ValueError("模型目录响应不符合 OpenAI 兼容格式")
    return [item for item in body["data"] if isinstance(item, dict) and item.get("id")]


def connectivity_error(error: httpx.HTTPError | ModelGatewayError | ValueError) -> tuple[int | None, str]:
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code, f"上游返回 HTTP {status_code}"
    if isinstance(error, httpx.TimeoutException):
        return None, "连接上游超时"
    if isinstance(error, httpx.RequestError):
        return None, "无法连接模型平台"
    return None, str(error)


@router.get("/readiness", response_model=ReadinessPublic)
async def readiness(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ReadinessPublic:
    present = await default_model_types(session, admin.tenant_id)
    configured_resolutions = await configured_image_resolutions(session, admin.tenant_id)
    required = [ModelType.TEXT, ModelType.IMAGE, ModelType.VIDEO]
    return ReadinessPublic(
        ready=all(item in present for item in required),
        required_defaults={item: item in present for item in required},
        optional_defaults={ModelType.TTS: ModelType.TTS in present},
        image_resolution_models={
            resolution: resolution in configured_resolutions
            for resolution in IMAGE_RESOLUTIONS
        },
        missing=[item for item in required if item not in present],
    )


@router.get("/pricing-rules", response_model=list[PricingRulePublic])
async def list_pricing_rules(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[PricingRule]:
    return list(
        (
            await session.scalars(
                select(PricingRule)
                .where(PricingRule.tenant_id == admin.tenant_id)
                .order_by(PricingRule.display_order, PricingRule.name)
            )
        ).all()
    )


@router.patch("/pricing-rules/{task_type}", response_model=PricingRulePublic)
async def update_pricing_rule(
    task_type: str,
    payload: PricingRuleUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PricingRule:
    rule = await session.scalar(
        select(PricingRule).where(
            PricingRule.tenant_id == admin.tenant_id,
            PricingRule.task_type == task_type,
        )
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="计费规则不存在")
    rule.unit_cost = payload.unit_cost
    rule.version += 1
    await session.commit()
    await session.refresh(rule)
    return rule


@router.get("/providers", response_model=list[ProviderPublic])
async def list_providers(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ProviderPublic]:
    providers = (
        await session.scalars(
            select(Provider).where(Provider.tenant_id == admin.tenant_id).order_by(Provider.name)
        )
    ).all()
    return [provider_public(item) for item in providers]


@router.post("/providers", response_model=ProviderPublic, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ProviderPublic:
    provider = Provider(
        tenant_id=admin.tenant_id,
        code=payload.code,
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=normalize_provider_base_url(payload.code, str(payload.base_url)),
        encrypted_api_key=SecretBox().encrypt(payload.api_key) if payload.api_key else None,
        encrypted_credentials=None,
        extra_headers=payload.extra_headers,
        adapter_config=payload.adapter_config,
        max_concurrency=payload.max_concurrency,
        enabled=payload.enabled,
    )
    credentials = validate_provider_credentials(
        payload.adapter_config,
        normalize_provider_credential_values(payload.code, payload.credentials),
        require_required=payload.enabled,
    )
    if credentials:
        provider.encrypted_credentials = SecretBox().encrypt(json.dumps(credentials, ensure_ascii=False))
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    return provider_public(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderPublic)
async def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ProviderPublic:
    provider = await tenant_record(session, Provider, provider_id, admin.tenant_id)
    values = payload.model_dump(
        exclude_unset=True,
        exclude={"api_key", "clear_api_key", "credentials", "clear_credentials"},
    )
    if provider.enabled and values.get("enabled") is False:
        enabled_model_id = await session.scalar(
            select(AIModel.id)
            .where(
                AIModel.tenant_id == admin.tenant_id,
                AIModel.provider_id == provider.id,
                AIModel.enabled.is_(True),
            )
            .limit(1)
        )
        if enabled_model_id:
            raise HTTPException(
                status_code=409,
                detail="平台下仍有启用模型，请先迁移或停用这些模型",
            )
    if "base_url" in values:
        values["base_url"] = normalize_provider_base_url(provider.code, str(values["base_url"]))
    for field, value in values.items():
        setattr(provider, field, value)
    if payload.clear_api_key:
        provider.encrypted_api_key = None
    elif payload.api_key is not None:
        provider.encrypted_api_key = SecretBox().encrypt(payload.api_key)
    next_credentials = {} if payload.clear_credentials else provider_credentials(provider)
    if payload.credentials is not None:
        next_credentials.update(
            {
                key: value
                for key, value in normalize_provider_credential_values(
                    provider.code, payload.credentials
                ).items()
                if value.strip()
            }
        )
    if (
        payload.credentials is not None
        or payload.clear_credentials
        or payload.adapter_config is not None
        or values.get("enabled") is True
    ):
        next_adapter = values.get("adapter_config", provider.adapter_config)
        next_credentials = validate_provider_credentials(
            next_adapter,
            next_credentials,
            require_required=provider.enabled,
        )
        provider.encrypted_credentials = (
            SecretBox().encrypt(json.dumps(next_credentials, ensure_ascii=False))
            if next_credentials
            else None
        )
    await session.commit()
    await session.refresh(provider)
    return provider_public(provider)


@router.post("/provider-presets/autodl-minimax-h3/install", status_code=status.HTTP_200_OK)
async def install_autodl_minimax_h3_preset(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, ProviderPublic | ModelPublic]:
    provider = await session.scalar(
        select(Provider).where(
            Provider.tenant_id == admin.tenant_id,
            Provider.code == AUTODL_MINIMAX_H3_PROVIDER_CODE,
        )
    )
    if provider is None:
        provider = Provider(
            tenant_id=admin.tenant_id,
            code=AUTODL_MINIMAX_H3_PROVIDER_CODE,
            name="AutoDL MiniMax H3 多图生视频",
            provider_type=ProviderType.CUSTOM,
            base_url="https://autodl.art",
            extra_headers={},
            adapter_config=autodl_minimax_h3_adapter_config(),
            max_concurrency=2,
            enabled=False,
        )
        session.add(provider)
        await session.flush()

    model = await session.scalar(
        select(AIModel).where(
            AIModel.provider_id == provider.id,
            AIModel.model_type == ModelType.VIDEO,
            AIModel.model_id == AUTODL_MINIMAX_H3_MODEL_ID,
        )
    )
    if model is None:
        model = AIModel(
            tenant_id=admin.tenant_id,
            provider_id=provider.id,
            model_id=AUTODL_MINIMAX_H3_MODEL_ID,
            name="MiniMax H3 多图生视频 15秒",
            model_type=ModelType.VIDEO,
            capabilities=autodl_minimax_h3_capabilities(),
            enabled=False,
            is_default=False,
        )
        session.add(model)
    await session.commit()
    await session.refresh(provider)
    await session.refresh(model)
    return {
        "provider": provider_public(provider),
        "model": ModelPublic.model_validate(model),
    }


@router.post("/provider-presets/agnes-ai/install", status_code=status.HTTP_200_OK)
async def install_agnes_ai_preset(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Install Agnes' OpenAI-compatible text/image API and custom async video API."""
    provider = await session.scalar(
        select(Provider).where(
            Provider.tenant_id == admin.tenant_id,
            Provider.code == AGNES_PROVIDER_CODE,
        )
    )
    if provider is None:
        provider = Provider(
            tenant_id=admin.tenant_id,
            code=AGNES_PROVIDER_CODE,
            name="Agnes AI",
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            base_url="https://apihub.agnes-ai.com/v1",
            extra_headers={},
            adapter_config=agnes_video_adapter_config(),
            max_concurrency=2,
            enabled=False,
        )
        session.add(provider)
        await session.flush()
    else:
        provider.base_url = provider.base_url or "https://apihub.agnes-ai.com/v1"
        provider.adapter_config = agnes_video_adapter_config()

    definitions = [
        (AGNES_TEXT_MODEL_ID, "Agnes 2.5 Flash", ModelType.TEXT, {}),
        (
            AGNES_IMAGE_21_MODEL_ID,
            "Agnes Image 2.1 Flash",
            ModelType.IMAGE,
            agnes_image_21_capabilities(),
        ),
        (AGNES_IMAGE_MODEL_ID, "Agnes Image 2.5 Flash", ModelType.IMAGE, agnes_image_capabilities()),
        (AGNES_VIDEO_MODEL_ID, "Agnes Video 2.5 Flash", ModelType.VIDEO, agnes_video_capabilities()),
    ]
    installed: list[AIModel] = []
    for model_id, name, model_type, capabilities in definitions:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.provider_id == provider.id,
                AIModel.model_id == model_id,
            )
        )
        if model is None:
            model = AIModel(
                tenant_id=admin.tenant_id,
                provider_id=provider.id,
                model_id=model_id,
                name=name,
                model_type=model_type,
                capabilities=capabilities,
                enabled=False,
                is_default=False,
            )
            session.add(model)
        elif model_type in {ModelType.IMAGE, ModelType.VIDEO}:
            model.capabilities = capabilities
        installed.append(model)
    await session.commit()
    await session.refresh(provider)
    for model in installed:
        await session.refresh(model)
    return {
        "provider": provider_public(provider),
        "models": [ModelPublic.model_validate(model) for model in installed],
    }


@router.post("/providers/{provider_id}/test", response_model=ConnectivityResult)
async def test_provider(
    provider_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ConnectivityResult:
    provider = await tenant_record(session, Provider, provider_id, admin.tenant_id)
    started = time.perf_counter()
    status_code: int | None = None
    discovered: int | None = None
    try:
        adapter = (
            ProviderAdapterConfig.model_validate(provider.adapter_config)
            if provider.adapter_config
            else None
        )
        if adapter and adapter.connectivity:
            await provider_gateway(provider).test_adapter_connectivity()
            discovered = None
            message = "连接成功，自定义探测请求已通过"
        else:
            rows = await fetch_provider_models(provider)
            discovered = len(rows)
            message = "连接成功，模型目录可访问"
        status_code = 200
        ok = True
    except (httpx.HTTPError, ModelGatewayError, ValueError) as error:
        ok = False
        status_code, reason = connectivity_error(error)
        message = f"连接失败：{reason}"
    latency_ms = round((time.perf_counter() - started) * 1000)
    provider.last_tested_at = datetime.now(UTC)
    provider.last_test_ok = ok
    provider.last_test_message = message
    await session.commit()
    return ConnectivityResult(
        ok=ok,
        status_code=status_code,
        message=message,
        latency_ms=latency_ms,
        discovered_models=discovered,
    )


@router.post("/providers/{provider_id}/discover-models", response_model=ModelDiscoveryResponse)
async def discover_models(
    provider_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ModelDiscoveryResponse:
    provider = await tenant_record(session, Provider, provider_id, admin.tenant_id)
    try:
        rows = await fetch_provider_models(provider)
    except (httpx.HTTPError, ModelGatewayError, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"模型目录获取失败：{error}") from error
    imported_ids = set(
        (
            await session.scalars(
                select(AIModel.model_id).where(
                    AIModel.tenant_id == admin.tenant_id,
                    AIModel.provider_id == provider.id,
                )
            )
        ).all()
    )
    items = []
    for row in rows:
        model_id = str(row["id"])[:160]
        items.append(
            {
                "model_id": model_id,
                "name": str(row.get("name") or model_id)[:160],
                "owned_by": str(row["owned_by"])[:160] if row.get("owned_by") else None,
                "inferred_type": infer_model_type(model_id),
                "is_imported": model_id in imported_ids,
            }
        )
    return ModelDiscoveryResponse(provider_id=provider.id, items=items)


@router.post("/providers/{provider_id}/import-models", response_model=ModelImportResponse)
async def import_models(
    provider_id: str,
    payload: ModelImportRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ModelImportResponse:
    provider = await tenant_record(session, Provider, provider_id, admin.tenant_id)
    if not provider.enabled:
        raise HTTPException(status_code=409, detail="请先启用模型平台再导入模型")
    requested_ids = [item.model_id for item in payload.items]
    existing_ids = set(
        (
            await session.scalars(
                select(AIModel.model_id).where(
                    AIModel.provider_id == provider.id,
                    AIModel.model_id.in_(requested_ids),
                )
            )
        ).all()
    )
    imported: list[AIModel] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for item in payload.items:
        if item.model_id in existing_ids or item.model_id in seen:
            skipped.append(item.model_id)
            continue
        seen.add(item.model_id)
        model = AIModel(
            tenant_id=admin.tenant_id,
            provider_id=provider.id,
            **item.model_dump(),
        )
        session.add(model)
        imported.append(model)
    await session.commit()
    for model in imported:
        await session.refresh(model)
    return ModelImportResponse(imported=imported, skipped_model_ids=skipped)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    provider = await tenant_record(session, Provider, provider_id, admin.tenant_id)
    model_id = await session.scalar(select(AIModel.id).where(AIModel.provider_id == provider.id).limit(1))
    if model_id:
        raise HTTPException(status_code=409, detail="平台仍有关联模型，请先删除或迁移这些模型")
    await session.delete(provider)
    await session.commit()


@router.get("/models", response_model=list[ModelPublic])
async def list_models(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AIModel]:
    return list(
        (
            await session.scalars(
                select(AIModel)
                .where(AIModel.tenant_id == admin.tenant_id)
                .order_by(AIModel.model_type, AIModel.name)
            )
        ).all()
    )


@router.get(
    "/image-resolution-models",
    response_model=list[ImageResolutionModelRoutePublic],
)
async def list_image_resolution_models(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ImageResolutionModelRoute]:
    routes = list(
        (
            await session.scalars(
                select(ImageResolutionModelRoute).where(
                    ImageResolutionModelRoute.tenant_id == admin.tenant_id
                )
            )
        ).all()
    )
    order = {resolution: index for index, resolution in enumerate(IMAGE_RESOLUTIONS)}
    return sorted(routes, key=lambda item: order.get(item.resolution, len(order)))


@router.put(
    "/image-resolution-models/{resolution}",
    response_model=ImageResolutionModelRoutePublic,
)
async def set_image_resolution_model(
    resolution: str,
    payload: ImageResolutionModelRouteUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ImageResolutionModelRoute:
    normalized_resolution = resolution.strip().upper()
    if normalized_resolution not in IMAGE_RESOLUTIONS:
        raise HTTPException(status_code=422, detail="图片分辨率仅支持 1K、2K 或 4K")
    model = await tenant_record(session, AIModel, payload.model_id, admin.tenant_id)
    if model.model_type != ModelType.IMAGE or not model.enabled:
        raise HTTPException(status_code=422, detail="请选择当前租户已启用的图片模型")
    provider = await tenant_record(session, Provider, model.provider_id, admin.tenant_id)
    if not provider.enabled:
        raise HTTPException(status_code=409, detail="图片模型所属平台当前已停用")
    route = await session.scalar(
        select(ImageResolutionModelRoute).where(
            ImageResolutionModelRoute.tenant_id == admin.tenant_id,
            ImageResolutionModelRoute.resolution == normalized_resolution,
        )
    )
    if route is None:
        route = ImageResolutionModelRoute(
            tenant_id=admin.tenant_id,
            resolution=normalized_resolution,
            model_id=model.id,
        )
        session.add(route)
    else:
        route.model_id = model.id
    session.info.pop(f"default-model-types:{admin.tenant_id}", None)
    await session.commit()
    await session.refresh(route)
    return route


@router.post("/models", response_model=ModelPublic, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AIModel:
    provider = await tenant_record(session, Provider, payload.provider_id, admin.tenant_id)
    if payload.enabled and not provider.enabled:
        raise HTTPException(status_code=409, detail="已停用的平台不能创建启用模型")
    if payload.is_default and not payload.enabled:
        raise HTTPException(status_code=422, detail="默认模型必须保持启用")
    if payload.is_default:
        await session.execute(
            update(AIModel)
            .where(AIModel.tenant_id == admin.tenant_id, AIModel.model_type == payload.model_type)
            .values(is_default=False)
        )
    model = AIModel(tenant_id=admin.tenant_id, **payload.model_dump())
    session.add(model)
    await session.commit()
    await session.refresh(model)
    return model


@router.patch("/models/{model_id}", response_model=ModelPublic)
async def update_model(
    model_id: str,
    payload: ModelUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AIModel:
    model = await tenant_record(session, AIModel, model_id, admin.tenant_id)
    values = payload.model_dump(exclude_unset=True)
    if "capabilities" in values and model.model_type == ModelType.VIDEO:
        values["capabilities"] = normalize_video_capabilities(values["capabilities"])
    effective_default = values.get("is_default", model.is_default)
    effective_enabled = values.get("enabled", model.enabled)
    if (
        model.is_default
        and model.model_type in REQUIRED_DEFAULT_MODEL_TYPES
        and values.get("is_default") is False
    ):
        raise HTTPException(status_code=409, detail="请先将同类型的其他模型设为默认模型")
    if (
        model.is_default
        and model.model_type in REQUIRED_DEFAULT_MODEL_TYPES
        and values.get("enabled") is False
    ):
        raise HTTPException(status_code=409, detail="必填默认模型不能停用，请先切换默认模型")
    if effective_default and not effective_enabled:
        raise HTTPException(status_code=422, detail="默认模型必须保持启用")
    if values.get("enabled") is True and not model.enabled:
        provider = await tenant_record(session, Provider, model.provider_id, admin.tenant_id)
        if not provider.enabled:
            raise HTTPException(status_code=409, detail="请先启用模型所属平台")
    if model.enabled and values.get("enabled") is False:
        used_by = await active_model_reference_labels(session, model, admin.tenant_id)
        if used_by:
            raise HTTPException(
                status_code=409,
                detail=f"模型正被{'、'.join(used_by)}使用，请先迁移这些配置",
            )
    if values.get("is_default") is True:
        await session.execute(
            update(AIModel)
            .where(AIModel.tenant_id == admin.tenant_id, AIModel.model_type == model.model_type)
            .values(is_default=False)
        )
    for field, value in values.items():
        setattr(model, field, value)
    await session.commit()
    await session.refresh(model)
    return model


@router.post("/models/{model_id}/test", response_model=ConnectivityResult)
async def test_model(
    model_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ConnectivityResult:
    model = await tenant_record(session, AIModel, model_id, admin.tenant_id)
    provider = await tenant_record(session, Provider, model.provider_id, admin.tenant_id)
    started = time.perf_counter()
    status_code: int | None = None
    discovered: int | None = None
    try:
        adapter = (
            ProviderAdapterConfig.model_validate(provider.adapter_config)
            if provider.adapter_config
            else None
        )
        if model.model_type == ModelType.IMAGE:
            capabilities = model.capabilities if isinstance(model.capabilities, dict) else {}
            resolutions = capabilities.get("resolutions")
            aspect_ratios = capabilities.get("aspect_ratios")
            resolution = (
                str(resolutions[0])
                if isinstance(resolutions, list) and resolutions
                else "1K"
            )
            aspect_ratio = (
                str(aspect_ratios[0])
                if isinstance(aspect_ratios, list) and aspect_ratios
                else "1:1"
            )
            image_data = await provider_gateway(provider).generate_image(
                ImageGenerationRequest(
                    model=model.model_id,
                    prompt="A single red ceramic cup on a clean white background, product photo",
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    capabilities=capabilities,
                    idempotency_key=f"model-test-{model.id}-{int(started * 1000)}",
                )
            )
            discovered = None
            status_code = 200
            ok = True
            message = f"真实生图验证通过，已返回 {max(1, len(image_data) // 1024)} KB 图片"
        elif model.model_type == ModelType.VIDEO and adapter and adapter.video:
            validate_provider_credentials(
                provider.adapter_config,
                provider_credentials(provider),
                require_required=True,
            )
            normalize_video_capabilities(model.capabilities)
            discovered = None
            status_code = 200
            ok = True
            message = "视频适配协议、凭据与模型能力配置验证通过（未创建计费任务）"
        elif adapter and not adapter.catalog and adapter.connectivity:
            await provider_gateway(provider).test_adapter_connectivity()
            discovered = None
            status_code = 200
            ok = True
            message = "模型能力配置有效，供应商探测请求已通过"
        else:
            rows = await fetch_provider_models(provider)
            discovered = len(rows)
            status_code = 200
            ok = any(str(item.get("id")) == model.model_id for item in rows)
            message = "模型目录验证通过" if ok else "平台连接正常，但远程模型目录中未找到该模型 ID"
    except (httpx.HTTPError, ModelGatewayError, ValueError) as error:
        ok = False
        status_code, reason = connectivity_error(error)
        message = f"模型验证失败：{reason}"

    latency_ms = round((time.perf_counter() - started) * 1000)
    model.last_tested_at = datetime.now(UTC)
    model.last_test_ok = ok
    model.last_test_message = message
    model.last_test_latency_ms = latency_ms
    await session.commit()
    return ConnectivityResult(
        ok=ok,
        status_code=status_code,
        message=message,
        latency_ms=latency_ms,
        discovered_models=discovered,
    )


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    model_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    model = await tenant_record(session, AIModel, model_id, admin.tenant_id)
    references = {
        "默认配置": model.id if model.is_default else None,
        "图片分辨率路由": await session.scalar(
            select(ImageResolutionModelRoute.id)
            .where(
                ImageResolutionModelRoute.tenant_id == admin.tenant_id,
                ImageResolutionModelRoute.model_id == model.id,
            )
            .limit(1)
        ),
        "项目": await session.scalar(
            select(Project.id)
            .where(
                Project.tenant_id == admin.tenant_id,
                (Project.video_model_id == model.id) | (Project.image_model_id == model.id),
            )
            .limit(1)
        ),
        "Agent": await session.scalar(
            select(AgentProfile.id)
            .where(
                AgentProfile.tenant_id == admin.tenant_id,
                AgentProfile.text_model_id == model.id,
            )
            .limit(1)
        ),
        "历史任务": await session.scalar(
            select(AITask.id).where(AITask.tenant_id == admin.tenant_id, AITask.model_id == model.id).limit(1)
        ),
        "视频片段": await session.scalar(
            select(VideoClip.id)
            .where(VideoClip.tenant_id == admin.tenant_id, VideoClip.model_id == model.id)
            .limit(1)
        ),
        "音色绑定": await session.scalar(
            select(VoiceBinding.id)
            .where(
                VoiceBinding.tenant_id == admin.tenant_id,
                VoiceBinding.tts_model_id == model.id,
            )
            .limit(1)
        ),
        "配音片段": await session.scalar(
            select(AudioClip.id)
            .where(AudioClip.tenant_id == admin.tenant_id, AudioClip.model_id == model.id)
            .limit(1)
        ),
    }
    used_by = [label for label, reference in references.items() if reference]
    if used_by:
        raise HTTPException(status_code=409, detail=f"模型正被{'、'.join(used_by)}引用，无法删除")
    await session.delete(model)
    await session.commit()


@router.get("/handbooks", response_model=list[HandbookPublic])
async def list_handbooks(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[Handbook]:
    return list((await session.scalars(select(Handbook).where(Handbook.tenant_id == admin.tenant_id))).all())


@router.get("/handbooks/manifests", response_model=list[HandbookManifestPublic])
async def list_handbook_manifests(
    _admin: User = Depends(require_admin),
) -> list[dict[str, object]]:
    return [
        {
            "handbook_type": handbook_type,
            "files": [
                {
                    "key": item.key,
                    "filename": item.filename,
                    "label": item.label,
                    "purpose": item.purpose,
                }
                for item in handbook_manifest(handbook_type)
            ],
        }
        for handbook_type in (HandbookType.VISUAL, HandbookType.DIRECTOR)
    ]


@router.post("/handbooks", response_model=HandbookPublic, status_code=status.HTTP_201_CREATED)
async def create_handbook(
    payload: HandbookCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Handbook:
    values = payload.model_dump(exclude={"files"})
    handbook = Handbook(tenant_id=admin.tenant_id, skill_path="", **values)
    session.add(handbook)
    await session.flush()
    try:
        await run_in_threadpool(create_handbook_package, handbook, payload.files)
    except ValueError as error:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    await session.refresh(handbook)
    return handbook


async def ensure_handbook_can_be_disabled(
    session: AsyncSession,
    handbook: Handbook,
    enabled: bool,
) -> None:
    if not handbook.enabled or enabled:
        return
    project_reference = await session.scalar(
        select(Project.id)
        .where(
            Project.tenant_id == handbook.tenant_id,
            (
                Project.visual_handbook_id == handbook.id
                if handbook.handbook_type == HandbookType.VISUAL
                else Project.director_handbook_id == handbook.id
            ),
        )
        .limit(1)
    )
    if project_reference:
        raise HTTPException(status_code=409, detail="创作手册正被项目使用，请先为这些项目切换手册")


@router.patch("/handbooks/{handbook_id}", response_model=HandbookPublic)
async def update_handbook(
    handbook_id: str,
    payload: HandbookUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Handbook:
    handbook = await tenant_record(session, Handbook, handbook_id, admin.tenant_id)
    values = payload.model_dump(exclude_unset=True)
    if "enabled" in values:
        await ensure_handbook_can_be_disabled(session, handbook, values["enabled"])
    for field, value in values.items():
        setattr(handbook, field, value)
    handbook.version += 1
    await session.commit()
    await session.refresh(handbook)
    return handbook


@router.get("/handbooks/{handbook_id}/package", response_model=HandbookPackagePublic)
async def get_handbook_package(
    handbook_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    handbook = await tenant_record(session, Handbook, handbook_id, admin.tenant_id)
    files = await run_in_threadpool(read_handbook_files, handbook)
    await session.commit()
    return {"handbook": handbook, "files": files}


@router.put("/handbooks/{handbook_id}/package", response_model=HandbookPackagePublic)
async def update_handbook_package(
    handbook_id: str,
    payload: HandbookPackageUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    handbook = await tenant_record(session, Handbook, handbook_id, admin.tenant_id)
    await ensure_handbook_can_be_disabled(session, handbook, payload.enabled)
    try:
        await run_in_threadpool(replace_handbook_files, handbook, payload.files)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    handbook.name = payload.name
    handbook.description = payload.description
    handbook.enabled = payload.enabled
    handbook.version += 1
    await session.commit()
    await session.refresh(handbook)
    return {"handbook": handbook, "files": await run_in_threadpool(read_handbook_files, handbook)}


@router.post("/handbooks/{handbook_id}/cover/upload", response_model=HandbookPublic)
async def upload_handbook_cover(
    handbook_id: str,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Handbook:
    handbook = await tenant_record(session, Handbook, handbook_id, admin.tenant_id)
    if file.content_type not in ALLOWED_COVER_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 JPG、PNG 或 WebP 图片")

    data = await file.read(MAX_COVER_BYTES + 1)
    await file.close()
    if len(data) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="封面图片不能超过 8 MB")
    if not data:
        raise HTTPException(status_code=422, detail="上传的图片为空")

    try:
        _local_url, stored_path = await run_in_threadpool(
            save_handbook_cover,
            data,
            uploads_root=get_settings().uploads_root,
            tenant_id=admin.tenant_id,
            handbook_id=handbook.id,
        )
    except InvalidCoverImage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        storage_key, cover_url = await persist_media_file(stored_path, "image/webp")
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise

    previous_storage_key = object_key_from_media_url(handbook.cover_url)
    handbook.cover_url = cover_url
    handbook.version += 1
    try:
        await session.commit()
    except Exception:
        await delete_media_file(storage_key, stored_path)
        await session.rollback()
        raise
    await session.refresh(handbook)
    if previous_storage_key and previous_storage_key != storage_key:
        try:
            await delete_media_file(previous_storage_key)
        except Exception:
            logger.warning(
                "Failed to delete replaced handbook cover %s",
                previous_storage_key,
                exc_info=True,
            )
    return handbook


@router.get("/agents", response_model=list[AgentProfilePublic])
async def list_agents(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AgentProfile]:
    return list(
        (await session.scalars(select(AgentProfile).where(AgentProfile.tenant_id == admin.tenant_id))).all()
    )


@router.post("/agents", response_model=AgentProfilePublic, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentProfileCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AgentProfile:
    if payload.text_model_id:
        model = await tenant_record(session, AIModel, payload.text_model_id, admin.tenant_id)
        if model.model_type != ModelType.TEXT:
            raise HTTPException(status_code=422, detail="Agent 只能绑定文本模型")
    agent = AgentProfile(tenant_id=admin.tenant_id, **payload.model_dump())
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@router.put("/agents/{agent_id}", response_model=AgentProfilePublic)
async def replace_agent(
    agent_id: str,
    payload: AgentProfileCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AgentProfile:
    agent = await tenant_record(session, AgentProfile, agent_id, admin.tenant_id)
    if payload.text_model_id:
        model = await tenant_record(session, AIModel, payload.text_model_id, admin.tenant_id)
        if model.model_type != ModelType.TEXT:
            raise HTTPException(status_code=422, detail="Agent 只能绑定文本模型")
    for field, value in payload.model_dump().items():
        setattr(agent, field, value)
    agent.version += 1
    await session.commit()
    await session.refresh(agent)
    return agent


@router.get("/prompts", response_model=list[PromptTemplatePublic])
async def list_prompts(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[PromptTemplate]:
    prompts = list(
        (
            await session.scalars(
                select(PromptTemplate).where(
                    PromptTemplate.tenant_id == admin.tenant_id,
                    PromptTemplate.code.in_(SYSTEM_PROMPT_CODES),
                )
            )
        ).all()
    )
    order = {code: index for index, (code, _name, _description) in enumerate(SYSTEM_PROMPTS)}
    return sorted(prompts, key=lambda prompt: order.get(prompt.code, len(order)))


@router.put("/prompts/{prompt_id}", response_model=PromptTemplatePublic)
async def replace_prompt(
    prompt_id: str,
    payload: PromptTemplateUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PromptTemplate:
    prompt = await tenant_record(session, PromptTemplate, prompt_id, admin.tenant_id)
    if prompt.code not in SYSTEM_PROMPT_CODES:
        raise HTTPException(status_code=404, detail="系统提示词不存在")
    prompt.content = payload.content.strip()
    prompt.version += 1
    await run_in_threadpool(write_prompt_file, prompt)
    await session.commit()
    await session.refresh(prompt)
    return prompt
