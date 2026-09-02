from __future__ import annotations

from pathlib import Path

from app.core.config import PROJECT_ROOT, Settings


def test_cors_origins_accepts_plain_docker_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:8080")

    settings = Settings(_env_file=None)

    assert settings.cors_origins == ["http://localhost:8080"]


def test_cors_origins_accepts_comma_separated_values(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://studio.example.com, https://admin.example.com",
    )

    settings = Settings(_env_file=None)

    assert settings.cors_origins == [
        "https://studio.example.com",
        "https://admin.example.com",
    ]


def test_cors_origins_accepts_json_for_older_container_images(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:8080"]')

    settings = Settings(_env_file=None)

    assert settings.cors_origins == ["http://localhost:8080"]


def test_relative_runtime_paths_are_anchored_to_project_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SKILLS_ROOT", raising=False)
    monkeypatch.delenv("UPLOADS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)

    assert settings.database_url == f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'cineforge.db').as_posix()}"
    assert settings.skills_root == PROJECT_ROOT / "skills"
    assert settings.uploads_root == PROJECT_ROOT / "uploads"


def test_relative_explicit_sqlite_url_is_anchored_to_project_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./runtime/worker.db")
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)

    assert settings.database_url == (
        f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'runtime' / 'worker.db').as_posix()}"
    )
