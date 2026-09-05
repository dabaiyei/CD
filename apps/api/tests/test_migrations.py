from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]


def migration_environment(database: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "APP_ENV": "production",
        "SEED_DEMO_DATA": "false",
    }


def run_alembic(database: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=API_ROOT,
        env=migration_environment(database),
        check=True,
        capture_output=True,
        text=True,
    )


def run_revision_guard(database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from app.db.migration_guard import assert_database_current; "
            "asyncio.run(assert_database_current())",
        ],
        cwd=API_ROOT,
        env=migration_environment(database),
        check=False,
        capture_output=True,
        text=True,
    )


def test_initial_migration_round_trip_and_revision_guard(tmp_path: Path) -> None:
    database = tmp_path / "migration-round-trip.db"

    missing_guard = run_revision_guard(database)
    assert missing_guard.returncode != 0
    assert "数据库尚未执行迁移" in missing_guard.stderr

    run_alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        task_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('ai_tasks')")
        }
        model_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('ai_models')")
        }
        provider_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('providers')")
        }
        asset_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('assets')")
        }
        revision_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('asset_revisions')")
        }
        task_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('ai_tasks')")
        }
        notification_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('notifications')")
        }
        memory_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('agent_memories')")
        }
        session_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info('agent_chat_sessions')")
        }
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('users')")
        }
        tenant_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('tenants')")
        }
        image_route_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('image_resolution_model_routes')"
            )
        }
        workflow_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('director_workflow_runs')")
        }
    assert {
        "tenants",
        "ai_tasks",
        "chapter_analyses",
        "video_clips",
        "dialogue_versions",
        "dialogue_lines",
        "voice_bindings",
        "audio_clips",
        "composition_versions",
        "auth_login_guards",
            "security_events",
        "pricing_rules",
        "asset_revisions",
            "script_reviews",
            "agent_chat_summaries",
            "director_workflow_runs",
            "director_child_runs",
            "director_decision_requests",
            "user_skills",
            "personal_agent_attachments",
            "image_resolution_model_routes",
            "invitation_codes",
            "invitation_redemptions",
            "user_templates",
            "marketplace_listings",
            "marketplace_acquisitions",
            "platform_branding",
            } <= tables
    assert {
        "worker_id",
        "lease_expires_at",
        "heartbeat_at",
        "started_at",
        "completed_at",
        "idempotency_key",
        "provider_job_id",
    } <= task_columns
    assert {
        "last_tested_at",
        "last_test_ok",
        "last_test_message",
        "last_test_latency_ms",
    } <= model_columns
    assert {"encrypted_credentials", "adapter_config", "max_concurrency"} <= provider_columns
    assert "lineage_id" in asset_columns
    assert {
        "asset_id",
        "version",
        "change_type",
        "source_task_id",
        "source_revision_id",
        "parent_asset_id",
    } <= revision_columns
    assert {
        "ix_ai_tasks_project_type_status",
        "ix_ai_tasks_tenant_user_created",
        "ix_ai_tasks_tenant_created",
        "ix_ai_tasks_status_lease",
    } <= task_indexes
    assert "ix_notifications_tenant_user_unread_created" in notification_indexes
    assert {
        "embedding",
        "embedding_model",
        "salience",
        "is_automatic",
        "source_session_id",
        "source_message_id",
        "last_accessed_at",
        "access_count",
    } <= memory_columns
    assert session_columns["project_id"][3] == 0
    assert {"avatar_url", "avatar_storage_path"} <= user_columns
    assert "invite_url_prefix" in tenant_columns
    assert "ix_image_resolution_routes_tenant_model" in image_route_indexes
    assert {"automation_mode", "stop_requested"} <= workflow_columns
    assert revision == ("e4a7c1d9b620",)

    checked = run_alembic(database, "check")
    assert "No new upgrade operations detected" in checked.stdout + checked.stderr

    guard = run_revision_guard(database)
    assert guard.returncode == 0, guard.stderr

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'stale-revision'")
    stale_guard = run_revision_guard(database)
    assert stale_guard.returncode != 0
    assert "数据库版本不匹配" in stale_guard.stderr
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'e4a7c1d9b620'")

    run_alembic(database, "downgrade", "base")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert tables == {"alembic_version"}
