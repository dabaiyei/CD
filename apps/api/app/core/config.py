from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _absolute_sqlite_url(value: str) -> str:
    prefixes = ("sqlite:///", "sqlite+aiosqlite:///")
    prefix = next((item for item in prefixes if value.startswith(item)), None)
    if prefix is None:
        return value
    database = value.removeprefix(prefix)
    if database == ":memory:" or database.startswith("file:"):
        return value
    database_path = Path(database)
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path
    return f"{prefix}{database_path.resolve().as_posix()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "CineForge API"
    app_env: str = "development"
    api_prefix: str = "/api/v1"
    database_url: str = f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'cineforge.db').as_posix()}"
    jwt_secret: str = "development-only-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "cineforge-api"
    jwt_audience: str = "cineforge-web"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    login_rate_limit: int = 30
    login_rate_window_seconds: int = 5 * 60
    credential_encryption_secret: str = "development-only-credential-secret"
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    seed_demo_data: bool = True
    skills_root: Path = Path("skills")
    uploads_root: Path = Path("uploads")
    storage_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "cineforge"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str = "us-east-1"
    s3_auto_create_bucket: bool = False
    media_signing_secret: str = "development-only-media-signing-secret"
    require_signed_media_urls: bool = False
    provider_test_timeout_seconds: float = 15.0
    agent_runtime_url: str = "http://127.0.0.1:8010"
    agent_runtime_internal_token: str = "development-agent-runtime-token"
    agent_runtime_timeout_seconds: float = 900.0
    redis_url: str | None = None
    task_queue_name: str = "cineforge:tasks"
    task_event_channel: str = "cineforge:events"
    task_poll_interval_seconds: float = 1.5
    worker_concurrency: int = Field(default=8, ge=1, le=64)
    task_recovery_interval_seconds: float = 60.0
    task_lease_timeout_seconds: float = 15 * 60
    task_heartbeat_seconds: float = 20.0
    agent_chat_task_timeout_seconds: float = Field(default=300.0, gt=0, le=300.0)
    media_request_timeout_seconds: float = 180.0
    allow_private_media_urls: bool = False

    @field_validator("database_url", mode="after")
    @classmethod
    def absolute_database_url(cls, value: str) -> str:
        return _absolute_sqlite_url(value)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                decoded = json.loads(stripped)
                if isinstance(decoded, list):
                    return decoded
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("skills_root", mode="after")
    @classmethod
    def absolute_skills_root(cls, value: Path) -> Path:
        return value.resolve() if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("uploads_root", mode="after")
    @classmethod
    def absolute_uploads_root(cls, value: Path) -> Path:
        return value.resolve() if value.is_absolute() else (PROJECT_ROOT / value).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
