from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_RUNTIME_",
        env_file=(".env", "../../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    internal_token: str = "development-agent-runtime-token"
    data_root: Path = Path("runtime-data")
    max_concurrent_runs: int = 2
    request_timeout_seconds: float = 900.0
    max_react_iterations: int = 30
    model_context_size: int = 128_000

    @property
    def absolute_data_root(self) -> Path:
        return self.data_root.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
