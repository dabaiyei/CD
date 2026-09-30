"""Exercise shared worker lifecycle races against a real SQLite database."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import AIModel, AITask, Provider, TaskEvent, TaskStatus
from app.services import task_worker as worker


async def database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    async with engine.begin() as connection:
        for model in (Provider, AIModel, AITask, TaskEvent):
            await connection.run_sync(model.__table__.create)
    async def no_publish(*args):
        return True
    monkeypatch.setattr(worker, "publish_task_event", no_publish)
    monkeypatch.setattr(worker, "enqueue_task", no_publish)
    return engine, sessions


async def seed(sessions, status, task_type="chapter_composition_render"):
    async with sessions() as session:
        session.add(AITask(id="task", tenant_id="t", user_id="u", task_type=task_type,
            status=status, worker_id=worker.WORKER_ID,
            heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
            lease_expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await session.commit()


@pytest.mark.parametrize("task_type", ["chapter_composition_render", "agent_memory_maintenance"])
def test_model_less_task_is_claimed_once(tmp_path, monkeypatch, task_type):
    async def run():
        engine, sessions = await database(tmp_path, monkeypatch)
        try:
            await seed(sessions, TaskStatus.QUEUED, task_type)
            claims = await asyncio.gather(*(worker._claim_task_candidate("task") for _ in range(6)))
            assert sum(claim is not None for claim in claims) == 1
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(TaskEvent)) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("task_type", ["director_storyboard_review", "shot_video_prompt_generation", "agent_chat_run"])
def test_two_recoverers_requeue_once(tmp_path, monkeypatch, task_type):
    async def run():
        engine, sessions = await database(tmp_path, monkeypatch)
        try:
            await seed(sessions, TaskStatus.RUNNING, task_type)
            results = await asyncio.gather(worker.recover_stale_tasks(), worker.recover_stale_tasks())
            assert sum(results) == 1
            async with sessions() as session:
                task = await session.get(AITask, "task")
                assert task.status == TaskStatus.QUEUED
                assert task.result_payload["recovery_count"] == 1
                assert await session.scalar(select(func.count()).select_from(TaskEvent)) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("finish", [True, False])
def test_recovery_does_not_overwrite_completed_or_renewed_task(tmp_path, monkeypatch, finish):
    async def run():
        engine, sessions = await database(tmp_path, monkeypatch)
        try:
            await seed(sessions, TaskStatus.RUNNING)
            async with sessions() as session:
                task = await worker.owned_task_for_update(session, "task")
                assert task is not None
                recovery = asyncio.create_task(worker.recover_stale_tasks())
                await asyncio.sleep(0.05)
                if finish:
                    task.status = TaskStatus.SUCCEEDED
                    task.result_payload = {"saved": True}
                else:
                    task.heartbeat_at = datetime.now(UTC)
                    task.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                await session.commit()
            assert await recovery == 0
            async with sessions() as session:
                task = await session.get(AITask, "task")
                assert task.status == (TaskStatus.SUCCEEDED if finish else TaskStatus.RUNNING)
                assert not (task.result_payload or {}).get("recovery_count")
        finally:
            await engine.dispose()
    asyncio.run(run())
