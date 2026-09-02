from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator, model_validator

from runtime import CONTRACT_VERSION, HARNESS_SDK_VERSION, RUNTIME_VERSION

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ContractModel(BaseModel):
    model_config = {"extra": "forbid"}


class ModelBinding(ContractModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl | None = None
    api_key: SecretStr
    reasoning_effort: str | None = Field(default=None, max_length=40)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)


class SkillSnapshot(ContractModel):
    path: str = Field(min_length=1, max_length=300)
    content: str = Field(max_length=1_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: str = Field(min_length=1, max_length=100)

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("skill path must be a normalized relative path")
        if not normalized.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml")):
            raise ValueError("skill file type is not allowed")
        return normalized


class ProjectFileSnapshot(ContractModel):
    id: str = Field(min_length=1, max_length=128)
    directory_id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=2_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    editable: bool

    @field_validator("id", "directory_id")
    @classmethod
    def validate_file_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not SAFE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("project file id is invalid")
        return value

    @field_validator("path")
    @classmethod
    def validate_project_file_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if (
            len(parts) != 3
            or parts[0] != "project-files"
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("project file path must be project-files/<id>/<file>")
        if not normalized.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml")):
            raise ValueError("project file type is not allowed")
        return normalized

    @model_validator(mode="after")
    def validate_path_scope(self) -> ProjectFileSnapshot:
        directory_id = self.directory_id or self.id
        if self.editable and directory_id != self.id:
            raise ValueError("editable project file directory must match its id")
        if self.path.split("/")[1] != directory_id:
            raise ValueError("project file path does not match its id")
        return self


class ProjectFileChange(ContractModel):
    operation: Literal["create", "update", "delete"]
    file_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=255)
    content: str | None = Field(default=None, max_length=2_000_000)
    base_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class AgentRunRequest(ContractModel):
    contract_version: Literal["v1", "v2"] = CONTRACT_VERSION
    tenant_id: str
    project_id: str
    task_id: str
    session_id: str
    prompt: str = Field(min_length=1, max_length=200_000)
    system_prompt: str = Field(min_length=1, max_length=200_000)
    model_binding: ModelBinding
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    skills: list[SkillSnapshot] = Field(default_factory=list, max_length=200)
    memory_context: list[str] = Field(default_factory=list, max_length=100)
    project_files: list[ProjectFileSnapshot] = Field(default_factory=list, max_length=200)

    @field_validator("tenant_id", "project_id", "task_id", "session_id")
    @classmethod
    def validate_scope_identifier(cls, value: str) -> str:
        if not SAFE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("scope identifiers may only contain letters, numbers, dot, dash and underscore")
        return value


class ExecutionManifest(ContractModel):
    runtime_type: Literal["deepseek-harness"] = "deepseek-harness"
    runtime_version: str = RUNTIME_VERSION
    harness_sdk_version: str = HARNESS_SDK_VERSION
    contract_version: str = CONTRACT_VERSION
    provider: str
    model: str
    prompt_versions: dict[str, str]
    skill_versions: dict[str, str]


class AgentRunResponse(ContractModel):
    session_id: str
    final_response: str
    finish_reason: str | None
    events: list[dict[str, Any]]
    manifest: ExecutionManifest
    project_file_changes: list[ProjectFileChange] = Field(default_factory=list, max_length=200)


class HealthResponse(ContractModel):
    status: Literal["ok"] = "ok"
    service: Literal["cineforge-agent-runtime"] = "cineforge-agent-runtime"
    runtime_version: str = RUNTIME_VERSION
    harness_sdk_version: str = HARNESS_SDK_VERSION
    contract_version: str = CONTRACT_VERSION
    harness_available: bool
