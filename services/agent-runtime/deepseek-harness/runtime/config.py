from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_RUNTIME_", extra="ignore")

    internal_token: str = "development-agent-runtime-token"
    data_root: Path = Path("runtime-data")
    profile: str = "sdk-minimal"
    max_concurrent_runs: int = 2
    initialize_timeout_seconds: float = 30.0
    request_timeout_seconds: float = 900.0

    @property
    def absolute_data_root(self) -> Path:
        return self.data_root.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
