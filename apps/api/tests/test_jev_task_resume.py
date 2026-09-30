import asyncio
import copy
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import AITask, TaskEvent, TaskStatus
from app.services import task_worker


def test_temporary_jev_outage_requeues_without_losing_paid_review_or_refunding(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jev-resume.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            for model in (AITask, TaskEvent):
                await conn.run_sync(model.__table__.create)
        checkpoint = {"total_units": 56, "completed": {
            str(i): {"approved": True, "summary": "已审核", "findings": []} for i in range(44)}}
        payload = {"chapter_id": "chapter", "storyboard_review_cache": copy.deepcopy(checkpoint)}
        async with sessions() as db:
            db.add(AITask(id="review", tenant_id="t", user_id="u", project_id="p",
                task_type="director_storyboard_review", status=TaskStatus.RUNNING,
                worker_id="owner", request_payload=payload, result_payload={"credit_refunded": False}))
            db.add(TaskEvent(id="progress", tenant_id="t", user_id="u", task_id="review",
                status=TaskStatus.RUNNING, progress=81, message="44/56 已保存", created_at=datetime.now(UTC)))
            await db.commit()
        events, queued = [], []

        async def owned(db, task_id):
            return await db.get(AITask, task_id)

        async def progress(task_id, value, message):
            events.append((value, message))

        async def enqueue(task_id):
            queued.append(task_id)

        async def publish(*args):
            pass

        monkeypatch.setattr(task_worker, "SessionLocal", sessions)
        monkeypatch.setattr(task_worker, "owned_task_for_update", owned)
        monkeypatch.setattr(task_worker, "owns_running_task", lambda t: t.status == TaskStatus.RUNNING)
        monkeypatch.setattr(task_worker, "record_progress", progress)
        monkeypatch.setattr(task_worker, "enqueue_task", enqueue)
        monkeypatch.setattr(task_worker, "publish_task_event", publish)
        await task_worker.requeue_after_jev_transient("review", "TLS 连接失败", delay_seconds=0)
        async with sessions() as db:
            task = await db.get(AITask, "review")
            assert task.status == TaskStatus.QUEUED
            assert task.request_payload == payload
            assert len(task.request_payload["storyboard_review_cache"]["completed"]) == 44
            assert task.result_payload["credit_refunded"] is False
            assert task.worker_id is None and task.lease_expires_at is None
            assert task.error_message is None
        assert queued == ["review"] and events[0][0] == 81

        # The public worker path must intercept this condition before calling
        # fail_task/refunds or advancing the workflow into WAITING_USER.
        from app.services import director_orchestration, jev_control
        original_requeue = task_worker.requeue_after_jev_transient

        async def execute(*args):
            raise jev_control.JevTransientPending("连接暂时失败")

        async def watch(*args):
            await args[-1].wait()

        async def unexpected(*args, **kwargs):
            raise AssertionError("Temporary JEV failure must not fail/advance the task")

        async def mark(*args):
            pass

        async def immediate_requeue(task_id, message):
            await original_requeue(task_id, message, delay_seconds=0)

        monkeypatch.setattr(task_worker, "execute_task", execute)
        monkeypatch.setattr(task_worker, "heartbeat_task", watch)
        monkeypatch.setattr(task_worker, "cancellation_watcher", watch)
        monkeypatch.setattr(task_worker, "fail_task", unexpected)
        monkeypatch.setattr(task_worker, "requeue_after_jev_transient", immediate_requeue)
        monkeypatch.setattr(director_orchestration, "mark_child_running", mark)
        monkeypatch.setattr(director_orchestration, "on_director_task_terminal", unexpected)
        async with sessions() as db:
            task = await db.get(AITask, "review")
            task.status = TaskStatus.RUNNING
            task.worker_id = "owner"
            await db.commit()
        await task_worker.run_claimed_task("review", lambda: None, lambda: None, lambda: None)
        assert queued == ["review", "review"]
        async with sessions() as db:
            task = await db.get(AITask, "review")
            assert task.status == TaskStatus.QUEUED and task.request_payload == payload
            assert task.result_payload["credit_refunded"] is False
        await engine.dispose()

    asyncio.run(run())
