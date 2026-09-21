from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

# SQLite serialises writers: only one connection may hold the write lock, and a
# second writer fails immediately with "database is locked" unless it is told
# to wait. The worker runs several task slots concurrently, so WAL (readers no
# longer block the writer) plus a busy timeout (writers queue instead of
# failing) are both required for a multi-slot worker on the default database.
SQLITE_BUSY_TIMEOUT_MS = 30_000

engine_options: dict[str, Any] = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000}
engine = create_async_engine(settings.database_url, **engine_options)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL persists in the database file, but busy_timeout is per connection.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app.db import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
