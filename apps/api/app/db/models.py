from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class UserRole(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class ProviderType(enum.StrEnum):
    SUB2API = "sub2api"
    NEW_API = "newapi"
    OPENAI_COMPATIBLE = "openai_compatible"
    CUSTOM = "custom"


class ModelType(enum.StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    TTS = "tts"


class HandbookType(enum.StrEnum):
    VISUAL = "visual"
    DIRECTOR = "director"


class UserSkillStage(enum.StrEnum):
    SCRIPT_GENERATION = "script_generation"
    SCRIPT_REVIEW = "script_review"
    ASSET_EXTRACTION = "asset_extraction"
    ASSET_PROMPT_GENERATION = "asset_prompt_generation"
    STORYBOARD_GENERATION = "storyboard_generation"
    STORYBOARD_REVIEW = "storyboard_review"
    VIDEO_GENERATION = "video_generation"


class MarketplaceResourceType(enum.StrEnum):
    SKILL = "skill"
    TEMPLATE = "template"
    MATERIAL = "material"


class AgentKind(enum.StrEnum):
    SCREENPLAY = "screenplay"
    GENERAL = "general"


class TaskStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentMessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class SourceMode(enum.StrEnum):
    NOVEL = "novel"
    SCRIPT = "script"


class ProjectFileKind(enum.StrEnum):
    SOURCE = "source"
    MEMORY = "memory"
    ANALYSIS = "analysis"
    SCRIPT = "script"
    ASSET = "asset"
    STORYBOARD = "storyboard"
    VIDEO = "video"
    AUDIO = "audio"
    OTHER = "other"


class ChapterStatus(enum.StrEnum):
    UNINITIALIZED = "uninitialized"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    SCRIPTING = "scripting"
    REVIEWING = "reviewing"
    ASSETS = "assets"
    STORYBOARD = "storyboard"
    VIDEO = "video"
    COMPLETED = "completed"


class AssetScope(enum.StrEnum):
    PROJECT = "project"
    GLOBAL = "global"


class AssetType(enum.StrEnum):
    CHARACTER = "character"
    SCENE = "scene"
    PROP = "prop"
    MATERIAL = "material"
    AUDIO = "audio"


class AssetStatus(enum.StrEnum):
    EXTRACTED = "extracted"
    PROMPT_READY = "prompt_ready"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class ScriptReviewDecision(enum.StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class DirectorWorkflowStage(enum.StrEnum):
    SCRIPT_ADAPTING = "script_adapting"
    SCRIPT_REVIEWING = "script_reviewing"
    AWAITING_SCRIPT_DECISION = "awaiting_script_decision"
    SCRIPT_REPAIRING = "script_repairing"
    ASSET_EXTRACTING = "asset_extracting"
    READY_FOR_ASSET_IMAGES = "ready_for_asset_images"
    ASSET_PREPARING = "asset_preparing"
    STORYBOARD_GENERATING = "storyboard_generating"
    STORYBOARD_REVIEWING = "storyboard_reviewing"
    AWAITING_STORYBOARD_DECISION = "awaiting_storyboard_decision"
    STORYBOARD_REPAIRING = "storyboard_repairing"
    VIDEO_PROMPT_GENERATING = "video_prompt_generating"
    VIDEO_GENERATING = "video_generating"
    READY_FOR_VIDEO = "ready_for_video"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DirectorWorkflowStatus(enum.StrEnum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DirectorChildStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VideoClipStatus(enum.StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AudioClipStatus(enum.StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompositionStatus(enum.StrEnum):
    DRAFT = "draft"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    invite_url_prefix: Mapped[str | None] = mapped_column(String(500), nullable=True)

    users: Mapped[list[User]] = relationship(back_populates="tenant")


class PlatformBranding(Base, TimestampMixin):
    __tablename__ = "platform_branding"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    login_background_video_source: Mapped[str] = mapped_column(String(16), default="default")
    login_background_video_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    login_background_video_storage_path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    avatar_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    avatar_storage_path: Mapped[str | None] = mapped_column(String(600), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    projects: Mapped[list[Project]] = relationship(back_populates="owner")


class InvitationCode(Base, TimestampMixin):
    __tablename__ = "invitation_codes"
    __table_args__ = (
        Index("ix_invitation_codes_tenant_created", "tenant_id", "created_at"),
        Index("ix_invitation_codes_tenant_active", "tenant_id", "enabled", "deleted_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80), default="创作者邀请")
    max_registrations: Mapped[int] = mapped_column(Integer, default=1)
    registration_count: Mapped[int] = mapped_column(Integer, default=0)
    initial_credits: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class InvitationRedemption(Base):
    __tablename__ = "invitation_redemptions"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_invitation_redemption_user"),
        Index("ix_invitation_redemptions_invitation_created", "invitation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invitation_id: Mapped[str] = mapped_column(ForeignKey("invitation_codes.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    initial_credits: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    replaced_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("refresh_sessions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthLoginGuard(Base, TimestampMixin):
    __tablename__ = "auth_login_guards"

    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    subject_hash: Mapped[str] = mapped_column(String(64), index=True)
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    user_agent: Mapped[str] = mapped_column(String(500), default="")
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class Provider(Base, TimestampMixin):
    __tablename__ = "providers"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_provider_tenant_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    provider_type: Mapped[ProviderType] = mapped_column(Enum(ProviderType, native_enum=False))
    base_url: Mapped[str] = mapped_column(String(500))
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    adapter_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    models: Mapped[list[AIModel]] = relationship(back_populates="provider", cascade="all, delete-orphan")


class AIModel(Base, TimestampMixin):
    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_type", "model_id", name="uq_provider_model_type_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[str] = mapped_column(String(160))
    name: Mapped[str] = mapped_column(String(160))
    model_type: Mapped[ModelType] = mapped_column(Enum(ModelType, native_enum=False), index=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_test_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    provider: Mapped[Provider] = relationship(back_populates="models")


class ImageResolutionModelRoute(Base, TimestampMixin):
    __tablename__ = "image_resolution_model_routes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "resolution", name="uq_image_resolution_route_tenant_resolution"),
        Index("ix_image_resolution_routes_tenant_model", "tenant_id", "model_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    resolution: Mapped[str] = mapped_column(String(8))
    model_id: Mapped[str] = mapped_column(ForeignKey("ai_models.id"), index=True)

    model: Mapped[AIModel] = relationship(foreign_keys=[model_id])


class Handbook(Base, TimestampMixin):
    __tablename__ = "handbooks"
    __table_args__ = (UniqueConstraint("tenant_id", "handbook_type", "name", name="uq_handbook_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    handbook_type: Mapped[HandbookType] = mapped_column(Enum(HandbookType, native_enum=False), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    skill_path: Mapped[str] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    video_model_id: Mapped[str | None] = mapped_column(ForeignKey("ai_models.id"), nullable=True)
    video_resolution: Mapped[str] = mapped_column(String(32), default="720p")
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="16:9")
    image_model_id: Mapped[str | None] = mapped_column(ForeignKey("ai_models.id"), nullable=True)
    image_resolution: Mapped[str] = mapped_column(String(8), default="1K")
    visual_handbook_id: Mapped[str | None] = mapped_column(ForeignKey("handbooks.id"), nullable=True)
    director_handbook_id: Mapped[str | None] = mapped_column(ForeignKey("handbooks.id"), nullable=True)

    owner: Mapped[User] = relationship(back_populates="projects")
    video_model: Mapped[AIModel | None] = relationship(foreign_keys=[video_model_id])
    image_model: Mapped[AIModel | None] = relationship(foreign_keys=[image_model_id])
    visual_handbook: Mapped[Handbook | None] = relationship(foreign_keys=[visual_handbook_id])
    director_handbook: Mapped[Handbook | None] = relationship(foreign_keys=[director_handbook_id])


class ProjectFile(Base, TimestampMixin):
    __tablename__ = "project_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[ProjectFileKind] = mapped_column(
        Enum(ProjectFileKind, native_enum=False), default=ProjectFileKind.OTHER, index=True
    )
    mime_type: Mapped[str] = mapped_column(String(160), default="text/plain")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(600), nullable=True)
    editable: Mapped[bool] = mapped_column(Boolean, default=True)
    file_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Chapter(Base, TimestampMixin):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("source_file_id", "order_index", name="uq_chapter_source_order"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    source_file_id: Mapped[str] = mapped_column(
        ForeignKey("project_files.id", ondelete="CASCADE"), index=True
    )
    source_mode: Mapped[SourceMode] = mapped_column(Enum(SourceMode, native_enum=False), index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    original_content: Mapped[str] = mapped_column(Text)
    status: Mapped[ChapterStatus] = mapped_column(
        Enum(ChapterStatus, native_enum=False), default=ChapterStatus.UNINITIALIZED, index=True
    )
    active_script_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("script_versions.id", ondelete="SET NULL", use_alter=True), nullable=True
    )


class ChapterAnalysis(Base, TimestampMixin):
    __tablename__ = "chapter_analyses"
    __table_args__ = (UniqueConstraint("chapter_id", "version", name="uq_chapter_analysis_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScriptVersion(Base, TimestampMixin):
    __tablename__ = "script_versions"
    __table_args__ = (UniqueConstraint("chapter_id", "version", name="uq_script_chapter_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ScriptReview(Base):
    __tablename__ = "script_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    script_version_id: Mapped[str] = mapped_column(
        ForeignKey("script_versions.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[ScriptReviewDecision] = mapped_column(
        Enum(ScriptReviewDecision, native_enum=False), index=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    reviewer_name: Mapped[str] = mapped_column(String(80))
    activated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DirectorWorkflowRun(Base, TimestampMixin):
    __tablename__ = "director_workflow_runs"
    __table_args__ = (
        Index("ix_director_workflows_chapter_updated", "chapter_id", "updated_at"),
        Index("ix_director_workflows_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    chat_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage: Mapped[DirectorWorkflowStage] = mapped_column(
        Enum(DirectorWorkflowStage, native_enum=False), index=True
    )
    status: Mapped[DirectorWorkflowStatus] = mapped_column(
        Enum(DirectorWorkflowStatus, native_enum=False), default=DirectorWorkflowStatus.RUNNING, index=True
    )
    automation_mode: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    script_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("script_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    asset_extraction_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_extractions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    storyboard_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("storyboard_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    current_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_message: Mapped[str] = mapped_column(String(1000), default="")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DirectorChildRun(Base, TimestampMixin):
    __tablename__ = "director_child_runs"
    __table_args__ = (Index("ix_director_child_workflow_created", "workflow_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("director_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    parent_child_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("director_child_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_tasks.id", ondelete="SET NULL"), nullable=True, unique=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[DirectorChildStatus] = mapped_column(
        Enum(DirectorChildStatus, native_enum=False), default=DirectorChildStatus.QUEUED, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    summary: Mapped[str] = mapped_column(String(1000), default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DirectorDecisionRequest(Base, TimestampMixin):
    __tablename__ = "director_decision_requests"
    __table_args__ = (Index("ix_director_decisions_workflow_created", "workflow_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("director_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    child_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("director_child_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decision_type: Mapped[str] = mapped_column(String(80), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    selected_option: Mapped[str | None] = mapped_column(String(80), nullable=True)
    feedback: Mapped[str] = mapped_column(Text, default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scope: Mapped[AssetScope] = mapped_column(Enum(AssetScope, native_enum=False), index=True)
    asset_type: Mapped[AssetType] = mapped_column(Enum(AssetType, native_enum=False), index=True)
    parent_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lineage_id: Mapped[str] = mapped_column(String(36), default=new_id, index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    generation_prompt: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus, native_enum=False), default=AssetStatus.EXTRACTED, index=True
    )
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AssetRevision(Base):
    __tablename__ = "asset_revisions"
    __table_args__ = (UniqueConstraint("asset_id", "version", name="uq_asset_revision_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[str] = mapped_column(String(80), index=True)
    source_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parent_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    generation_prompt: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    status: Mapped[AssetStatus] = mapped_column(Enum(AssetStatus, native_enum=False), index=True)
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AssetExtraction(Base, TimestampMixin):
    __tablename__ = "asset_extractions"
    __table_args__ = (UniqueConstraint("chapter_id", "version", name="uq_asset_extraction_chapter_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    script_version_id: Mapped[str] = mapped_column(
        ForeignKey("script_versions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class AssetExtractionItem(Base):
    __tablename__ = "asset_extraction_items"
    __table_args__ = (UniqueConstraint("extraction_id", "asset_id", name="uq_asset_extraction_item"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    extraction_id: Mapped[str] = mapped_column(
        ForeignKey("asset_extractions.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)


class StoryboardVersion(Base, TimestampMixin):
    __tablename__ = "storyboard_versions"
    __table_args__ = (UniqueConstraint("chapter_id", "version", name="uq_storyboard_chapter_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    script_version_id: Mapped[str] = mapped_column(
        ForeignKey("script_versions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class StoryboardShot(Base, TimestampMixin):
    __tablename__ = "storyboard_shots"
    __table_args__ = (
        UniqueConstraint("storyboard_version_id", "order_index", name="uq_storyboard_shot_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    storyboard_version_id: Mapped[str] = mapped_column(
        ForeignKey("storyboard_versions.id", ondelete="CASCADE"), index=True
    )
    order_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    shot_type: Mapped[str] = mapped_column(String(80), default="中景")
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("5"))
    scene_description: Mapped[str] = mapped_column(Text, default="")
    action_description: Mapped[str] = mapped_column(Text, default="")
    dialogue: Mapped[str] = mapped_column(Text, default="")
    image_prompt: Mapped[str] = mapped_column(Text, default="")
    video_prompt: Mapped[str] = mapped_column(Text, default="")
    asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reference_image_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class VideoClip(Base, TimestampMixin):
    __tablename__ = "video_clips"
    __table_args__ = (UniqueConstraint("shot_id", "version", name="uq_video_clip_shot_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    storyboard_version_id: Mapped[str] = mapped_column(
        ForeignKey("storyboard_versions.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[str] = mapped_column(ForeignKey("storyboard_shots.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("ai_models.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[VideoClipStatus] = mapped_column(
        Enum(VideoClipStatus, native_enum=False), default=VideoClipStatus.QUEUED, index=True
    )
    media_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class DialogueVersion(Base, TimestampMixin):
    __tablename__ = "dialogue_versions"
    __table_args__ = (UniqueConstraint("chapter_id", "version", name="uq_dialogue_chapter_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    script_version_id: Mapped[str] = mapped_column(
        ForeignKey("script_versions.id", ondelete="CASCADE"), index=True
    )
    storyboard_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("storyboard_versions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    runtime_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class DialogueLine(Base, TimestampMixin):
    __tablename__ = "dialogue_lines"
    __table_args__ = (UniqueConstraint("dialogue_version_id", "order_index", name="uq_dialogue_line_order"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    dialogue_version_id: Mapped[str] = mapped_column(
        ForeignKey("dialogue_versions.id", ondelete="CASCADE"), index=True
    )
    shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("storyboard_shots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_index: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(String(120), default="自然")
    direction: Mapped[str] = mapped_column(Text, default="")
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class VoiceBinding(Base, TimestampMixin):
    __tablename__ = "voice_bindings"
    __table_args__ = (
        UniqueConstraint("project_id", "character_asset_id", name="uq_voice_project_character"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    character_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    tts_model_id: Mapped[str] = mapped_column(ForeignKey("ai_models.id"), index=True)
    provider_voice_id: Mapped[str] = mapped_column(String(255))
    provider_voice_name: Mapped[str] = mapped_column(String(255), default="")
    style: Mapped[str] = mapped_column(String(255), default="自然")
    instructions: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AudioClip(Base, TimestampMixin):
    __tablename__ = "audio_clips"
    __table_args__ = (UniqueConstraint("dialogue_line_id", "version", name="uq_audio_line_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    dialogue_version_id: Mapped[str] = mapped_column(
        ForeignKey("dialogue_versions.id", ondelete="CASCADE"), index=True
    )
    dialogue_line_id: Mapped[str] = mapped_column(
        ForeignKey("dialogue_lines.id", ondelete="CASCADE"), index=True
    )
    voice_binding_id: Mapped[str] = mapped_column(
        ForeignKey("voice_bindings.id", ondelete="RESTRICT"), index=True
    )
    model_id: Mapped[str] = mapped_column(ForeignKey("ai_models.id"), index=True)
    line_version: Mapped[int] = mapped_column(Integer)
    binding_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[AudioClipStatus] = mapped_column(
        Enum(AudioClipStatus, native_enum=False), default=AudioClipStatus.QUEUED, index=True
    )
    media_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CompositionVersion(Base, TimestampMixin):
    __tablename__ = "composition_versions"
    __table_args__ = (UniqueConstraint("chapter_id", "version", name="uq_composition_chapter_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    storyboard_version_id: Mapped[str] = mapped_column(
        ForeignKey("storyboard_versions.id", ondelete="CASCADE"), index=True
    )
    dialogue_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dialogue_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    resolution: Mapped[str] = mapped_column(String(32), default="1080p")
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="16:9")
    fps: Mapped[int] = mapped_column(Integer, default=24)
    timeline_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[CompositionStatus] = mapped_column(
        Enum(CompositionStatus, native_enum=False), default=CompositionStatus.DRAFT, index=True
    )
    output_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CreditAccount(Base, TimestampMixin):
    __tablename__ = "credit_accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_credit_account_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    reason: Mapped[str] = mapped_column(String(120))
    reference_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PricingRule(Base, TimestampMixin):
    __tablename__ = "pricing_rules"
    __table_args__ = (UniqueConstraint("tenant_id", "task_type", name="uq_pricing_rule_task_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    task_type: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")
    unit_label: Mapped[str] = mapped_column(String(40), default="次")
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AITask(Base, TimestampMixin):
    __tablename__ = "ai_tasks"
    __table_args__ = (
        Index("ix_ai_tasks_project_type_status", "project_id", "task_type", "status"),
        Index("ix_ai_tasks_tenant_user_created", "tenant_id", "user_id", "created_at"),
        Index("ix_ai_tasks_tenant_created", "tenant_id", "created_at"),
        Index("ix_ai_tasks_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, native_enum=False), default=TaskStatus.QUEUED)
    model_id: Mapped[str | None] = mapped_column(ForeignKey("ai_models.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(80), nullable=True, unique=True, index=True
    )
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("ai_tasks.id", ondelete="CASCADE"), index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, native_enum=False), index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(500))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "ix_notifications_tenant_user_unread_created",
            "tenant_id",
            "user_id",
            "is_read",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(40), default="system", index=True)
    title: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(String(1000), default="")
    notification_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AgentProfile(Base, TimestampMixin):
    __tablename__ = "agent_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "name", name="uq_agent_profile_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    kind: Mapped[AgentKind] = mapped_column(Enum(AgentKind, native_enum=False), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text)
    text_model_id: Mapped[str | None] = mapped_column(ForeignKey("ai_models.id"), nullable=True)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class PromptTemplate(Base, TimestampMixin):
    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_prompt_template_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class UserSkill(Base, TimestampMixin):
    __tablename__ = "user_skills"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "name", name="uq_user_skill_name"),
        Index("ix_user_skills_owner_enabled", "tenant_id", "user_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    trigger_stages: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class UserTemplate(Base, TimestampMixin):
    __tablename__ = "user_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "name", name="uq_user_template_name"),
        Index("ix_user_templates_owner_updated", "tenant_id", "user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="通用", index=True)
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)


class MarketplaceListing(Base, TimestampMixin):
    __tablename__ = "marketplace_listings"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "publisher_user_id",
            "source_id",
            name="uq_marketplace_listing_source",
        ),
        Index(
            "ix_marketplace_listings_public_feed",
            "resource_type",
            "published",
            "updated_at",
        ),
        Index("ix_marketplace_listings_publisher", "publisher_user_id", "published"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resource_type: Mapped[MarketplaceResourceType] = mapped_column(
        Enum(MarketplaceResourceType, native_enum=False), index=True
    )
    publisher_tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    publisher_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(160), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="通用", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    cover_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MarketplaceAcquisition(Base, TimestampMixin):
    __tablename__ = "marketplace_acquisitions"
    __table_args__ = (
        UniqueConstraint("listing_id", "user_id", name="uq_marketplace_acquisition_user"),
        Index("ix_marketplace_acquisitions_owner", "tenant_id", "user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    listing_id: Mapped[str] = mapped_column(
        ForeignKey("marketplace_listings.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    listing_version: Mapped[int] = mapped_column(Integer)


class AgentMemory(Base, TimestampMixin):
    __tablename__ = "agent_memories"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "project_id", "namespace", "memory_key", name="uq_memory_scope"
        ),
        Index(
            "ix_agent_memories_retrieval_scope",
            "tenant_id",
            "user_id",
            "project_id",
            "namespace",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    namespace: Mapped[str] = mapped_column(String(80), default="default")
    memory_key: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    memory_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(384).with_variant(JSON, "sqlite"), nullable=True
    )
    embedding_model: Mapped[str] = mapped_column(String(120), default="cineforge-hash-v1")
    salience: Mapped[float] = mapped_column(Float, default=0.5)
    is_automatic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_chat_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, default=0)


class AgentChatSession(Base, TimestampMixin):
    __tablename__ = "agent_chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_profile_id: Mapped[str] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(160), default="新对话")
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    runtime_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    messages: Mapped[list[AgentChatMessage]] = relationship(
        back_populates="chat_session", cascade="all, delete-orphan"
    )


class AgentChatMessage(Base, TimestampMixin):
    __tablename__ = "agent_chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[AgentMessageRole] = mapped_column(Enum(AgentMessageRole, native_enum=False))
    content: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    finish_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    runtime_events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    runtime_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    chat_session: Mapped[AgentChatSession] = relationship(back_populates="messages")


class PersonalAgentAttachment(Base, TimestampMixin):
    __tablename__ = "personal_agent_attachments"
    __table_args__ = (
        Index("ix_personal_agent_attachments_owner", "tenant_id", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_chat_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_chat_messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(String(600))
    media_url: Mapped[str] = mapped_column(String(600))


class AgentChatSummary(Base, TimestampMixin):
    __tablename__ = "agent_chat_summaries"
    __table_args__ = (
        UniqueConstraint("session_id", "version", name="uq_agent_chat_summary_version"),
        Index("ix_agent_chat_summaries_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_chat_sessions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    through_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    model_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True
    )
    source_message_count: Mapped[int] = mapped_column(Integer, default=0)
    source_char_count: Mapped[int] = mapped_column(Integer, default=0)
    summary_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
