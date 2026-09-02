from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.db.models import (
    AgentKind,
    AgentMessageRole,
    AssetScope,
    AssetStatus,
    AssetType,
    AudioClipStatus,
    ChapterStatus,
    CompositionStatus,
    DirectorChildStatus,
    DirectorWorkflowStage,
    DirectorWorkflowStatus,
    HandbookType,
    MarketplaceResourceType,
    ModelType,
    ProjectFileKind,
    ProviderType,
    ScriptReviewDecision,
    SourceMode,
    TaskStatus,
    UserRole,
    UserSkillStage,
    VideoClipStatus,
)
from app.services.provider_adapters import normalize_adapter_config, normalize_video_capabilities


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    tenant: str | None = None
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserPublic(ApiModel):
    id: str
    tenant_id: str
    email: str
    display_name: str
    role: UserRole
    is_active: bool
    avatar_url: str | None = None


class AdminUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.USER
    initial_credits: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("请输入有效邮箱")
        return normalized

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return value.strip()


class InvitationSettingsPublic(BaseModel):
    url_prefix: str


class InvitationSettingsUpdate(BaseModel):
    url_prefix: str = Field(min_length=8, max_length=500)

    @field_validator("url_prefix")
    @classmethod
    def normalize_url_prefix(cls, value: str) -> str:
        return value.strip().rstrip("/")


class PlatformBrandingPublic(BaseModel):
    login_background_video_url: str
    login_background_video_source: Literal["default", "url", "upload"]
    updated_at: datetime | None = None


class LoginBackgroundVideoUrlUpdate(BaseModel):
    url: str = Field(min_length=8, max_length=2000)

    @field_validator("url")
    @classmethod
    def validate_video_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请输入有效的 HTTP 或 HTTPS 视频地址")
        if parsed.username or parsed.password:
            raise ValueError("视频地址不能包含用户名或密码")
        return normalized


class InvitationCreate(BaseModel):
    name: str = Field(default="创作者邀请", min_length=1, max_length=80)
    max_registrations: int = Field(default=1, ge=1, le=100_000)
    initial_credits: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=14, decimal_places=2
    )
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("请输入邀请名称")
        return normalized


class InvitationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    max_registrations: int | None = Field(default=None, ge=1, le=100_000)
    initial_credits: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("请输入邀请名称")
        return normalized


class InvitationPublic(ApiModel):
    id: str
    code: str
    name: str
    max_registrations: int
    registration_count: int
    remaining_registrations: int
    initial_credits: Decimal
    enabled: bool
    invite_url: str | None
    created_at: datetime
    updated_at: datetime


class InvitationRegistrationInfo(BaseModel):
    code: str
    tenant_name: str
    invitation_name: str
    initial_credits: Decimal
    remaining_registrations: int


class InvitationRegistrationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("请输入有效邮箱")
        return normalized

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return value.strip()


class AdminUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: UserRole | None = None
    is_active: bool | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class AdminPasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class AdminCreditAdjustment(BaseModel):
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    reason: str = Field(min_length=2, max_length=120)

    @field_validator("amount")
    @classmethod
    def require_non_zero_amount(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("积分调整不能为 0")
        return value

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return value.strip()


class AdminUserPublic(ApiModel):
    id: str
    tenant_id: str
    email: str
    display_name: str
    role: UserRole
    is_active: bool
    credit_balance: Decimal
    project_count: int
    task_count: int
    last_task_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminUserPage(BaseModel):
    items: list[AdminUserPublic]
    total: int
    active_count: int
    admin_count: int
    total_balance: Decimal


class CreditLedgerPublic(ApiModel):
    id: str
    user_id: str
    amount: Decimal
    balance_after: Decimal
    reason: str
    reference_type: str | None
    reference_id: str | None
    created_at: datetime


class CreditLedgerPage(BaseModel):
    items: list[CreditLedgerPublic]
    next_before: datetime | None = None
    next_before_id: str | None = None


class AdminCreditAdjustmentResult(BaseModel):
    user: AdminUserPublic
    ledger: CreditLedgerPublic


class SessionPublic(BaseModel):
    user: UserPublic
    tenant_name: str
    tenant_slug: str
    credit_balance: Decimal


class SecurityEventPublic(ApiModel):
    id: str
    user_id: str | None
    event_type: str
    success: bool
    subject_hash: str
    ip_hash: str
    user_agent: str
    event_metadata: dict[str, Any]
    created_at: datetime


class SecurityEventPage(BaseModel):
    items: list[SecurityEventPublic]
    next_before: datetime | None = None
    next_before_id: str | None = None


class ProviderCreate(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    name: str = Field(min_length=1, max_length=120)
    provider_type: ProviderType
    base_url: HttpUrl
    api_key: str | None = Field(default=None, max_length=4096)
    credentials: dict[str, str] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    adapter_config: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int = Field(default=2, ge=1, le=64)
    enabled: bool = True

    @field_validator("adapter_config")
    @classmethod
    def validate_adapter_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return normalize_adapter_config(value)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider_type: ProviderType | None = None
    base_url: HttpUrl | None = None
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    credentials: dict[str, str] | None = None
    clear_credentials: bool = False
    extra_headers: dict[str, str] | None = None
    adapter_config: dict[str, Any] | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=64)
    enabled: bool | None = None

    @field_validator("adapter_config")
    @classmethod
    def validate_adapter_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return normalize_adapter_config(value) if value is not None else None


class ProviderPublic(ApiModel):
    id: str
    code: str
    name: str
    provider_type: ProviderType
    base_url: str
    extra_headers: dict[str, str]
    adapter_config: dict[str, Any]
    configured_credentials: list[str]
    max_concurrency: int
    enabled: bool
    has_api_key: bool
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_test_message: str | None
    created_at: datetime
    updated_at: datetime


class ConnectivityResult(BaseModel):
    ok: bool
    status_code: int | None = None
    message: str
    latency_ms: int
    discovered_models: int | None = None


class DiscoveredModel(BaseModel):
    model_id: str
    name: str
    owned_by: str | None = None
    inferred_type: ModelType
    is_imported: bool = False


class ModelDiscoveryResponse(BaseModel):
    provider_id: str
    items: list[DiscoveredModel]


class ModelImportItem(BaseModel):
    model_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    model_type: ModelType
    capabilities: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_capabilities_for_type(self) -> ModelImportItem:
        if self.model_type == ModelType.VIDEO:
            self.capabilities = normalize_video_capabilities(self.capabilities)
        return self


class ModelImportRequest(BaseModel):
    items: list[ModelImportItem] = Field(min_length=1, max_length=500)


class ModelImportResponse(BaseModel):
    imported: list[ModelPublic]
    skipped_model_ids: list[str]


class ModelCreate(BaseModel):
    provider_id: str
    model_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    model_type: ModelType
    capabilities: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False

    @model_validator(mode="after")
    def validate_capabilities_for_type(self) -> ModelCreate:
        if self.model_type == ModelType.VIDEO:
            self.capabilities = normalize_video_capabilities(self.capabilities)
        return self


class ModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    capabilities: dict[str, Any] | None = None
    enabled: bool | None = None
    is_default: bool | None = None


class ModelPublic(ApiModel):
    id: str
    provider_id: str
    model_id: str
    name: str
    model_type: ModelType
    capabilities: dict[str, Any]
    enabled: bool
    is_default: bool
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_test_message: str | None
    last_test_latency_ms: int | None
    created_at: datetime
    updated_at: datetime


class ImageResolutionModelRouteUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=36)


class ImageResolutionModelRoutePublic(ApiModel):
    id: str
    resolution: Literal["1K", "2K", "4K"]
    model_id: str
    created_at: datetime
    updated_at: datetime


class HandbookCreate(BaseModel):
    handbook_type: HandbookType
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    cover_url: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    files: dict[str, str] | None = None


class HandbookUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    cover_url: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class HandbookPackageUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True
    files: dict[str, str]


class HandbookSkillFilePublic(BaseModel):
    key: str
    filename: str
    label: str
    purpose: str
    content: str


class HandbookPackagePublic(BaseModel):
    handbook: HandbookPublic
    files: list[HandbookSkillFilePublic]


class HandbookManifestFilePublic(BaseModel):
    key: str
    filename: str
    label: str
    purpose: str


class HandbookManifestPublic(BaseModel):
    handbook_type: HandbookType
    files: list[HandbookManifestFilePublic]


class HandbookPublic(ApiModel):
    id: str
    handbook_type: HandbookType
    name: str
    description: str
    cover_url: str | None
    skill_path: str
    version: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    cover_url: str | None = Field(default=None, max_length=500)
    video_model_id: str = Field(min_length=1, max_length=36)
    video_resolution: str = Field(default="720p", max_length=32)
    aspect_ratio: str = Field(default="16:9", max_length=16)
    image_model_id: None = None
    image_resolution: Literal["1K", "2K", "4K"] = "1K"
    visual_handbook_id: str = Field(min_length=1, max_length=36)
    director_handbook_id: str = Field(min_length=1, max_length=36)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    cover_url: str | None = Field(default=None, max_length=500)
    video_model_id: str | None = None
    video_resolution: str | None = Field(default=None, max_length=32)
    aspect_ratio: str | None = Field(default=None, max_length=16)
    image_model_id: None = None
    image_resolution: Literal["1K", "2K", "4K"] | None = None
    visual_handbook_id: str | None = None
    director_handbook_id: str | None = None


class ProjectPublic(ApiModel):
    id: str
    tenant_id: str
    owner_id: str
    name: str
    description: str
    cover_url: str | None
    video_model_id: str | None
    video_resolution: str
    aspect_ratio: str
    image_model_id: str | None
    image_resolution: str
    visual_handbook_id: str | None
    director_handbook_id: str | None
    created_at: datetime
    updated_at: datetime


class ProjectOptions(BaseModel):
    video_models: list[ModelPublic]
    visual_handbooks: list[HandbookPublic]
    director_handbooks: list[HandbookPublic]
    video_resolutions: list[str] = ["480p", "720p", "1080p"]
    aspect_ratios: list[str] = ["16:9", "9:16", "1:1", "4:3", "3:4"]
    image_resolutions: list[str] = ["1K", "2K", "4K"]


class ProjectFileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: ProjectFileKind = ProjectFileKind.OTHER
    content: str = Field(default="", max_length=2_000_000)


class ProjectFileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    kind: ProjectFileKind | None = None
    content: str | None = Field(default=None, max_length=2_000_000)


class ProjectFilePublic(ApiModel):
    id: str
    project_id: str
    name: str
    kind: ProjectFileKind
    mime_type: str
    size_bytes: int
    editable: bool
    file_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProjectFileDetail(ProjectFilePublic):
    content: str | None


class ChapterPublic(ApiModel):
    id: str
    project_id: str
    source_file_id: str
    source_mode: SourceMode
    order_index: int
    title: str
    original_content: str
    status: ChapterStatus
    active_script_version_id: str | None
    created_at: datetime
    updated_at: datetime


class ChapterAnalysisPublic(ApiModel):
    id: str
    project_id: str
    chapter_id: str
    version: int
    summary: str
    content: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ScriptGenerationRequest(BaseModel):
    analysis_id: str | None = None
    base_script_version_id: str | None = None


class ScriptVersionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=500_000)
    status: Literal["draft", "reviewing", "approved"] = "draft"
    review_notes: str = Field(default="", max_length=50_000)
    activate: bool = True


class ScriptVersionPublic(ApiModel):
    id: str
    project_id: str
    chapter_id: str
    version: int
    title: str
    content: str
    status: str
    review_notes: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ScriptReviewCreate(BaseModel):
    decision: ScriptReviewDecision
    notes: str = Field(default="", max_length=50_000)
    activate: bool = True


class ScriptReviewPublic(ApiModel):
    id: str
    script_version_id: str
    decision: ScriptReviewDecision
    notes: str
    reviewer_name: str
    activated: bool
    created_at: datetime


class ScriptReviewResult(BaseModel):
    review: ScriptReviewPublic
    script: ScriptVersionPublic


class DirectorWorkflowStart(BaseModel):
    instruction: str = Field(default="", max_length=20_000)
    chat_session_id: str | None = None


class DirectorDecisionSubmit(BaseModel):
    option: Literal["partial_repair", "full_rewrite", "continue_anyway", "provide_feedback"]
    feedback: str = Field(default="", max_length=20_000)


class DirectorChildRunPublic(ApiModel):
    id: str
    workflow_id: str
    parent_child_run_id: str | None
    task_id: str | None
    kind: str
    title: str
    status: DirectorChildStatus
    attempt: int
    max_attempts: int
    summary: str
    details: dict[str, Any]
    input_refs: dict[str, Any]
    output_refs: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DirectorDecisionPublic(ApiModel):
    id: str
    workflow_id: str
    child_run_id: str | None
    decision_type: str
    prompt: str
    options: list[dict[str, Any]]
    selected_option: str | None
    feedback: str
    resolved: bool
    created_at: datetime
    updated_at: datetime


class DirectorWorkflowPublic(ApiModel):
    id: str
    project_id: str
    chapter_id: str
    chat_session_id: str | None
    stage: DirectorWorkflowStage
    status: DirectorWorkflowStatus
    script_version_id: str | None
    asset_extraction_id: str | None
    storyboard_version_id: str | None
    current_task_id: str | None
    context_snapshot: dict[str, Any]
    last_message: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class DirectorWorkflowDetail(BaseModel):
    workflow: DirectorWorkflowPublic
    child_runs: list[DirectorChildRunPublic]
    pending_decision: DirectorDecisionPublic | None


class AssetCreate(BaseModel):
    asset_type: AssetType
    parent_asset_id: str | None = None
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=20_000)
    generation_prompt: str = Field(default="", max_length=50_000)
    asset_metadata: dict[str, Any] = Field(default_factory=dict)


class AssetUpdate(BaseModel):
    parent_asset_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=20_000)
    generation_prompt: str | None = Field(default=None, max_length=50_000)
    media_url: str | None = Field(default=None, max_length=600)
    asset_metadata: dict[str, Any] | None = None


class AssetPublic(ApiModel):
    id: str
    project_id: str | None
    scope: AssetScope
    asset_type: AssetType
    parent_asset_id: str | None
    name: str
    description: str
    generation_prompt: str
    media_url: str | None
    status: AssetStatus
    asset_metadata: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class AssetRevisionPublic(ApiModel):
    id: str
    asset_id: str
    version: int
    change_type: str
    source_task_id: str | None
    source_revision_id: str | None
    parent_asset_id: str | None
    name: str
    description: str
    generation_prompt: str
    media_url: str | None
    status: AssetStatus
    asset_metadata: dict[str, Any]
    created_at: datetime


class AssetExtractionCreate(BaseModel):
    assets: list[AssetCreate] = Field(min_length=1, max_length=500)


class AssetSelectionRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("asset_ids")
    @classmethod
    def unique_asset_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class AssetExtractionPublic(ApiModel):
    id: str
    project_id: str
    chapter_id: str
    script_version_id: str
    version: int
    is_active: bool
    invalidated_reason: str | None
    created_at: datetime
    updated_at: datetime


class AssetExtractionResult(BaseModel):
    extraction: AssetExtractionPublic
    assets: list[AssetPublic]


class StoryboardShotCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    shot_type: str = Field(default="中景", min_length=1, max_length=80)
    duration_seconds: Decimal = Field(default=Decimal("5"), ge=1, le=300)
    scene_description: str = Field(default="", max_length=20_000)
    action_description: str = Field(default="", max_length=20_000)
    dialogue: str = Field(default="", max_length=20_000)
    image_prompt: str = Field(default="", max_length=30_000)
    video_prompt: str = Field(default="", max_length=30_000)
    asset_ids: list[str] = Field(default_factory=list, max_length=100)
    reference_image_url: str | None = Field(default=None, max_length=600)

    @field_validator("asset_ids")
    @classmethod
    def unique_storyboard_asset_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class StoryboardShotUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    shot_type: str | None = Field(default=None, min_length=1, max_length=80)
    duration_seconds: Decimal | None = Field(default=None, ge=1, le=300)
    scene_description: str | None = Field(default=None, max_length=20_000)
    action_description: str | None = Field(default=None, max_length=20_000)
    dialogue: str | None = Field(default=None, max_length=20_000)
    image_prompt: str | None = Field(default=None, max_length=30_000)
    video_prompt: str | None = Field(default=None, max_length=30_000)
    asset_ids: list[str] | None = Field(default=None, max_length=100)
    reference_image_url: str | None = Field(default=None, max_length=600)

    @field_validator("asset_ids")
    @classmethod
    def unique_updated_storyboard_asset_ids(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class StoryboardShotBatchRequest(BaseModel):
    shot_ids: list[str] = Field(default_factory=list, max_length=300)
    overwrite: bool = False
    only_missing: bool = True

    @field_validator("shot_ids")
    @classmethod
    def unique_storyboard_shot_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class StoryboardShotPublic(ApiModel):
    id: str
    storyboard_version_id: str
    order_index: int
    title: str
    shot_type: str
    duration_seconds: Decimal
    scene_description: str
    action_description: str
    dialogue: str
    image_prompt: str
    video_prompt: str
    asset_ids: list[str]
    reference_image_url: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class StoryboardVersionCreate(BaseModel):
    shots: list[StoryboardShotCreate] = Field(min_length=1, max_length=200)


class StoryboardVersionPublic(ApiModel):
    id: str
    project_id: str
    chapter_id: str
    script_version_id: str
    version: int
    is_active: bool
    invalidated_reason: str | None
    created_at: datetime
    updated_at: datetime


class VideoClipPublic(ApiModel):
    id: str
    storyboard_version_id: str
    shot_id: str
    model_id: str
    version: int
    status: VideoClipStatus
    media_url: str | None
    provider_job_id: str | None
    error_message: str | None
    is_active: bool
    invalidated_reason: str | None
    created_at: datetime
    updated_at: datetime


class StoryboardVersionDetail(BaseModel):
    version: StoryboardVersionPublic
    shots: list[StoryboardShotPublic]
    video_clips: list[VideoClipPublic]


class DialogueVersionPublic(ApiModel):
    id: str
    project_id: str
    chapter_id: str
    script_version_id: str
    storyboard_version_id: str | None
    version: int
    runtime_manifest: dict[str, Any]
    is_active: bool
    invalidated_reason: str | None
    created_at: datetime
    updated_at: datetime


class DialogueLineUpdate(BaseModel):
    speaker: str | None = Field(default=None, min_length=1, max_length=160)
    text: str | None = Field(default=None, min_length=1, max_length=20_000)
    emotion: str | None = Field(default=None, max_length=120)
    direction: str | None = Field(default=None, max_length=20_000)


class DialogueLinePublic(ApiModel):
    id: str
    dialogue_version_id: str
    shot_id: str | None
    order_index: int
    speaker: str
    text: str
    emotion: str
    direction: str
    source_hash: str
    version: int
    created_at: datetime
    updated_at: datetime


class VoiceBindingUpsert(BaseModel):
    character_asset_id: str
    tts_model_id: str
    provider_voice_id: str = Field(min_length=1, max_length=255)
    provider_voice_name: str = Field(default="", max_length=255)
    style: str = Field(default="自然", max_length=255)
    instructions: str = Field(default="", max_length=20_000)
    enabled: bool = True


class VoiceBindingPublic(ApiModel):
    id: str
    project_id: str
    character_asset_id: str
    tts_model_id: str
    provider_voice_id: str
    provider_voice_name: str
    style: str
    instructions: str
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class AudioClipPublic(ApiModel):
    id: str
    dialogue_version_id: str
    dialogue_line_id: str
    voice_binding_id: str
    model_id: str
    line_version: int
    binding_version: int
    version: int
    status: AudioClipStatus
    media_url: str | None
    provider_job_id: str | None
    duration_seconds: Decimal | None
    error_message: str | None
    is_active: bool
    invalidated_reason: str | None
    created_at: datetime
    updated_at: datetime


class DialogueVersionDetail(BaseModel):
    version: DialogueVersionPublic
    lines: list[DialogueLinePublic]
    audio_clips: list[AudioClipPublic]


class DubbingOptions(BaseModel):
    tts_models: list[ModelPublic]
    character_assets: list[AssetPublic]
    voice_bindings: list[VoiceBindingPublic]


class CompositionPrepareRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    background_music_file_id: str | None = None
    environment_audio_file_ids: list[str] = Field(default_factory=list, max_length=8)
    dialogue_volume: Decimal = Field(default=Decimal("1"), ge=0, le=2)
    background_music_volume: Decimal = Field(default=Decimal("0.22"), ge=0, le=1)
    environment_volume: Decimal = Field(default=Decimal("0.35"), ge=0, le=1)
    fade_seconds: Decimal = Field(default=Decimal("0.35"), ge=0, le=2)
    fps: int = Field(default=24, ge=20, le=60)

    @field_validator("environment_audio_file_ids")
    @classmethod
    def unique_environment_files(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class CompositionVersionPublic(ApiModel):
    id: str
    project_id: str
    chapter_id: str
    storyboard_version_id: str
    dialogue_version_id: str | None
    version: int
    title: str
    duration_seconds: Decimal
    resolution: str
    aspect_ratio: str
    fps: int
    timeline_manifest: dict[str, Any]
    status: CompositionStatus
    output_url: str | None
    error_message: str | None
    is_active: bool
    invalidated_reason: str | None
    created_at: datetime
    updated_at: datetime


class FinishingOptions(BaseModel):
    audio_files: list[ProjectFilePublic]


class DialogueBatchGenerationRequest(BaseModel):
    dialogue_line_ids: list[str] | None = Field(default=None, max_length=200)

    @field_validator("dialogue_line_ids")
    @classmethod
    def unique_dialogue_line_ids(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class SourceImportResult(BaseModel):
    source_file: ProjectFilePublic
    original_file: ProjectFilePublic | None = None
    chapters: list[ChapterPublic]


class CoverGenerationRequest(BaseModel):
    style_hint: str | None = Field(default=None, max_length=500)


class TaskPublic(ApiModel):
    id: str
    project_id: str | None
    task_type: str
    status: TaskStatus
    model_id: str | None
    idempotency_key: str | None
    provider_job_id: str | None
    cost: Decimal
    request_payload: dict[str, Any]
    result_payload: dict[str, Any] | None
    error_message: str | None
    progress: int = 0
    latest_message: str | None = None
    latest_event_at: datetime | None = None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskPage(BaseModel):
    items: list[TaskPublic]
    next_before: datetime | None = None


class TaskEventPublic(ApiModel):
    id: str
    task_id: str
    status: TaskStatus
    progress: int
    message: str
    event_metadata: dict[str, Any]
    created_at: datetime


class PricingRulePublic(ApiModel):
    id: str
    task_type: str
    name: str
    description: str
    unit_label: str
    unit_cost: Decimal
    display_order: int
    version: int
    created_at: datetime
    updated_at: datetime


class PricingRuleUpdate(BaseModel):
    unit_cost: Decimal = Field(ge=0, le=1_000_000, max_digits=14, decimal_places=2)


class NotificationPublic(ApiModel):
    id: str
    project_id: str | None
    task_id: str | None
    category: str
    title: str
    message: str
    notification_metadata: dict[str, Any]
    is_read: bool
    created_at: datetime
    updated_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationPublic]
    unread_count: int


class AgentMemoryUpsert(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentMemoryPublic(ApiModel):
    id: str
    project_id: str | None
    namespace: str
    memory_key: str
    content: str
    memory_metadata: dict[str, Any]
    embedding_model: str
    salience: float
    is_automatic: bool
    source_session_id: str | None
    source_message_id: str | None
    last_accessed_at: datetime | None
    access_count: int
    created_at: datetime
    updated_at: datetime


class UserSkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    trigger_stages: list[UserSkillStage] = Field(min_length=1, max_length=7)
    description: str = Field(min_length=1, max_length=200_000)
    enabled: bool = True

    @field_validator("name", "description")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("field cannot be blank")
        return normalized

    @field_validator("trigger_stages")
    @classmethod
    def unique_trigger_stages(cls, value: list[UserSkillStage]) -> list[UserSkillStage]:
        return list(dict.fromkeys(value))


class UserSkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    trigger_stages: list[UserSkillStage] | None = Field(default=None, min_length=1, max_length=7)
    description: str | None = Field(default=None, min_length=1, max_length=200_000)
    enabled: bool | None = None

    @field_validator("name", "description")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("field cannot be blank")
        return normalized

    @field_validator("trigger_stages")
    @classmethod
    def unique_optional_trigger_stages(
        cls,
        value: list[UserSkillStage] | None,
    ) -> list[UserSkillStage] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class UserSkillPublic(ApiModel):
    id: str
    name: str
    trigger_stages: list[UserSkillStage]
    description: str
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class UserTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    category: str = Field(default="通用", min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=200_000)

    @field_validator("name", "category", "content")
    @classmethod
    def normalize_template_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_template_description(cls, value: str) -> str:
        return value.strip()


class UserTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)

    @field_validator("name", "category", "content")
    @classmethod
    def normalize_optional_template_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_optional_template_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class UserTemplatePublic(ApiModel):
    id: str
    name: str
    description: str
    category: str
    content: str
    version: int
    created_at: datetime
    updated_at: datetime


class MarketplacePublishRequest(BaseModel):
    category: str = Field(default="通用", min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("category")
    @classmethod
    def normalize_marketplace_category(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("分类不能为空")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_marketplace_tags(cls, value: list[str]) -> list[str]:
        normalized = [item.strip()[:30] for item in value if item.strip()]
        return list(dict.fromkeys(normalized))[:8]


class MarketplaceListingPublic(ApiModel):
    id: str
    resource_type: MarketplaceResourceType
    title: str
    description: str
    category: str
    tags: list[str]
    cover_url: str | None
    payload: dict[str, Any]
    version: int
    download_count: int
    publisher_name: str
    publisher_avatar_url: str | None
    owned_by_me: bool
    acquired: bool
    has_update: bool
    target_id: str | None
    published_at: datetime
    updated_at: datetime


class MarketplacePage(BaseModel):
    items: list[MarketplaceListingPublic]
    total: int
    categories: list[str]


class MarketplaceAcquisitionResult(BaseModel):
    listing_id: str
    target_type: str
    target_id: str
    listing_version: int
    created: bool


class AgentOptionPublic(ApiModel):
    id: str
    kind: AgentKind
    name: str
    description: str
    memory_enabled: bool


class AgentSkillPublic(BaseModel):
    id: str
    handbook_type: HandbookType
    name: str
    description: str
    version: int


class AgentChatOptions(BaseModel):
    agents: list[AgentOptionPublic]
    skills: list[AgentSkillPublic]


class AgentChatSessionCreate(BaseModel):
    scene: Literal["workspace", "director"] = "workspace"


class AgentChatSessionPublic(ApiModel):
    id: str
    project_id: str | None
    agent_profile_id: str
    title: str
    last_message_at: datetime
    runtime_manifest: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class AgentChatAttachmentPublic(BaseModel):
    id: str
    project_id: str | None
    name: str
    mime_type: str
    size_bytes: int
    media_url: str


class AgentChatMediaOptions(BaseModel):
    aspect_ratio: str | None = Field(default=None, min_length=3, max_length=20)
    resolution: str | None = Field(default=None, min_length=2, max_length=40)
    duration_seconds: float | None = Field(default=None, ge=1, le=300)


class AgentChatMessageCreate(BaseModel):
    content: str = Field(default="", max_length=200_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=36)
    mode: Literal["chat", "image", "video", "skill"] = "chat"
    skill_ids: list[str] = Field(default_factory=list, max_length=12)
    media_options: AgentChatMediaOptions = Field(default_factory=AgentChatMediaOptions)

    @field_validator("content")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        return value.strip()

    @field_validator("attachment_ids")
    @classmethod
    def unique_attachment_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("skill_ids")
    @classmethod
    def unique_skill_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def require_message_content(self) -> AgentChatMessageCreate:
        if not self.content and not self.attachment_ids:
            raise ValueError("message or image attachment is required")
        return self


class AgentChatMessagePublic(ApiModel):
    id: str
    session_id: str
    role: AgentMessageRole
    content: str
    run_id: str | None
    finish_reason: str | None
    runtime_events: list[dict[str, Any]]
    runtime_manifest: dict[str, Any] | None
    created_at: datetime


class AgentChatSessionDetail(BaseModel):
    session: AgentChatSessionPublic
    messages: list[AgentChatMessagePublic]
    active_task: TaskPublic | None = None


class AgentChatRunPublic(BaseModel):
    session: AgentChatSessionPublic
    user_message: AgentChatMessagePublic
    assistant_message: AgentChatMessagePublic


class AgentChatRunQueuedPublic(BaseModel):
    session: AgentChatSessionPublic
    user_message: AgentChatMessagePublic
    task: TaskPublic


class ReadinessPublic(BaseModel):
    ready: bool
    required_defaults: dict[ModelType, bool]
    optional_defaults: dict[ModelType, bool]
    image_resolution_models: dict[str, bool]
    missing: list[ModelType]


class AgentProfileCreate(BaseModel):
    kind: AgentKind
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    system_prompt: str = Field(min_length=1)
    text_model_id: str | None = None
    memory_enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AgentProfilePublic(ApiModel):
    id: str
    kind: AgentKind
    name: str
    description: str
    system_prompt: str
    text_model_id: str | None
    memory_enabled: bool
    config: dict[str, Any]
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class PromptTemplateUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt content cannot be blank")
        return value


class PromptTemplatePublic(ApiModel):
    id: str
    code: str
    name: str
    description: str
    content: str
    version: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SkillTreeNode(BaseModel):
    name: str
    path: str
    kind: Literal["directory", "file"]
    children: list[SkillTreeNode] = Field(default_factory=list)


class SkillFilePublic(BaseModel):
    path: str
    content: str


class SkillFileSave(BaseModel):
    content: str = Field(max_length=1_000_000)

    @field_validator("content")
    @classmethod
    def reject_null_bytes(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("skill files cannot contain null bytes")
        return value
