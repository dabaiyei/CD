from __future__ import annotations

from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import engine


def migration_script_directory() -> ScriptDirectory:
    migrations_root = Path(__file__).resolve().parents[2] / "migrations"
    if not migrations_root.is_dir():
        raise RuntimeError(f"数据库迁移目录不存在: {migrations_root}")
    return ScriptDirectory(str(migrations_root))


async def assert_database_current(database_engine: AsyncEngine = engine) -> None:
    expected_revision = migration_script_directory().get_current_head()
    try:
        async with database_engine.connect() as connection:
            actual_revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except SQLAlchemyError as exc:
        raise RuntimeError("数据库尚未执行迁移，请先运行 `alembic upgrade head`") from exc
    if actual_revision != expected_revision:
        raise RuntimeError(
            f"数据库版本不匹配: 当前 {actual_revision or 'base'}，代码要求 {expected_revision}；"
            "请先运行 `alembic upgrade head`"
        )
