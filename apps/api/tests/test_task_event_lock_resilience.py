from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db.session import SQLITE_BUSY_TIMEOUT_MS, engine
from app.services import task_worker


def _lock_error() -> OperationalError:
    return OperationalError(
        "INSERT INTO task_events (id, task_id) VALUES (?, ?)",
        ("event-1", "task-1"),
        Exception("sqlite3.OperationalError: database is locked"),
    )


class _RecordingSession:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.commits = 0

    async def __aenter__(self):
        if self.failures > 0:
            self.failures -= 1
            raise _lock_error()
        return self

    async def __aexit__(self, *_exc_info) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1


def test_record_progress_retries_transient_sqlite_lock(monkeypatch) -> None:
    session = _RecordingSession(failures=2)
    monkeypatch.setattr(task_worker, "SessionLocal", lambda: session)

    async def owned(session_arg, task_id):
        return object()

    monkeypatch.setattr(task_worker, "owned_task_for_update", owned)
    monkeypatch.setattr(task_worker, "owns_running_task", lambda task: True)
    monkeypatch.setattr(task_worker, "record_task_event", lambda *a, **k: object())

    published: list[object] = []

    async def publish(*_args, **_kwargs):
        published.append(object())

    monkeypatch.setattr(task_worker, "publish_task_event", publish)

    # Two lock failures are retried instead of failing the task that owns this
    # progress update; the third attempt commits normally.
    asyncio.run(task_worker.record_progress("task-1", 88, "designing techniques"))

    assert session.failures == 0
    assert session.commits == 1
    assert len(published) == 1


def test_record_progress_drops_permanent_operational_error(monkeypatch) -> None:
    session = _RecordingSession(failures=1)
    monkeypatch.setattr(task_worker, "SessionLocal", lambda: session)

    async def owned(session_arg, task_id):
        raise OperationalError("SELECT 1", {}, Exception("no such table: task_events"))

    monkeypatch.setattr(task_worker, "owned_task_for_update", owned)
    monkeypatch.setattr(task_worker, "owns_running_task", lambda task: True)

    # A non-lock operational error must not be retried forever, and must not
    # escape into the caller's task result.
    asyncio.run(task_worker.record_progress("task-1", 88, "designing techniques"))

    assert session.commits == 0


def test_safe_error_message_hides_sql_for_lock_errors() -> None:
    message = task_worker._safe_error_message(_lock_error())

    assert "INSERT INTO" not in message
    assert "task_events" not in message
    assert "数据库正忙" in message


def test_sqlite_engine_waits_for_the_write_lock() -> None:
    assert SQLITE_BUSY_TIMEOUT_MS == 30_000
    assert engine.sync_engine.pool._dialect.name == "sqlite"
    assert engine.dialect.name == "sqlite"


def test_sqlite_connections_enable_wal_and_busy_timeout() -> None:
    async def read_pragmas() -> dict[str, object]:
        async with engine.connect() as connection:
            values = {}
            for pragma in ("journal_mode", "busy_timeout", "foreign_keys"):
                values[pragma] = (await connection.execute(text(f"PRAGMA {pragma}"))).scalar()
            return values

    values = asyncio.run(read_pragmas())

    assert str(values["journal_mode"]).lower() == "wal"
    assert int(values["busy_timeout"]) == SQLITE_BUSY_TIMEOUT_MS
    assert int(values["foreign_keys"]) == 1
