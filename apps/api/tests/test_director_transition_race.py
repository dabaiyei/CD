"""Worker completion and periodic recovery must not enqueue two successors."""
import asyncio
import pytest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import session as db_session
from app.db.models import AITask, DirectorChildRun, DirectorWorkflowRun
from app.services import director_orchestration as service


@pytest.mark.parametrize("kind", ["storyboard_review", "storyboard_generation", "storyboard_repair", "video_prompt", "asset_image"])
def test_concurrent_terminal_notifications_advance_once(tmp_path, monkeypatch, kind):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db_session, "SessionLocal", sessions)
        async with engine.begin() as connection:
            for model in (AITask, DirectorWorkflowRun, DirectorChildRun):
                await connection.run_sync(model.__table__.create)
        owner = dict(tenant_id="t", user_id="u", project_id="p")
        async with sessions() as session:
            session.add(AITask(id="task", **owner, task_type=kind,
                status=service.TaskStatus.SUCCEEDED, result_payload={"approved": True}))
            session.add(DirectorWorkflowRun(id="workflow", **owner, chapter_id="chapter",
                stage=service.DirectorWorkflowStage.STORYBOARD_REVIEWING,
                status=service.DirectorWorkflowStatus.RUNNING, automation_mode=True,
                current_task_id="task", context_snapshot={"consecutive_image_failures": 4}))
            session.add(DirectorChildRun(id="child", **owner, workflow_id="workflow", task_id="task",
                kind=kind, title="review", status=service.DirectorChildStatus.RUNNING))
            await session.commit()
        enqueues = []

        async def queue(session, workflow, child=None):
            enqueues.append(workflow.id)
            # Let the second completion race while the first transaction is open.
            await asyncio.sleep(0.05)
            workflow.current_task_id = "next-task"
            return (None, None) if kind in {"storyboard_review", "asset_image"} else []

        async def dispatch(session, _queued):
            await session.commit()

        monkeypatch.setattr(service, "_queue_video_prompts", queue)
        monkeypatch.setattr(service, "_queue_asset_preparation", queue)
        monkeypatch.setattr(service, "_queue_video_generation", queue)
        async def assets(*args):
            return []
        monkeypatch.setattr(service, "_extraction_assets", assets)
        monkeypatch.setattr(service, "_after_asset_preparation", queue)
        monkeypatch.setattr(service, "_commit_and_dispatch", dispatch)
        try:
            await asyncio.gather(*(service._advance_director_task_terminal("task") for _ in range(3)))
            assert enqueues == ["workflow"]
            # A delayed start notification cannot resurrect a completed child.
            await service.mark_child_running("task")
            async with sessions() as session:
                child = await session.get(DirectorChildRun, "child")
                workflow = await session.get(DirectorWorkflowRun, "workflow")
                assert child.status == service.DirectorChildStatus.SUCCEEDED
                assert workflow.current_task_id == "next-task"
                if kind == "asset_image":
                    assert workflow.context_snapshot["consecutive_image_failures"] == 0
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("kind,attempt,failures,stops", [
    ("storyboard_review", 100, 0, False), ("storyboard_repair", 100, 0, False),
    ("script_adaptation", 100, 0, False), ("video_prompt", 100, 0, False),
    ("video_generation", 100, 0, False), ("asset_image", 4, 3, False),
    ("asset_image", 1, 4, True), ("asset_image", 100, 0, False),
])
def test_automatic_retry_limit_is_only_consecutive_image_failures(tmp_path, monkeypatch, kind, attempt, failures, stops):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db_session, "SessionLocal", sessions)
        async with engine.begin() as connection:
            for model in (AITask, DirectorWorkflowRun, DirectorChildRun):
                await connection.run_sync(model.__table__.create)
        owner = dict(tenant_id="t", user_id="u", project_id="p")
        async with sessions() as session:
            session.add(AITask(id="task", **owner, task_type=kind, status=service.TaskStatus.FAILED))
            session.add(DirectorWorkflowRun(id="workflow", **owner, chapter_id="chapter",
                stage=service.DirectorWorkflowStage.STORYBOARD_REVIEWING,
                status=service.DirectorWorkflowStatus.RUNNING, automation_mode=True,
                context_snapshot={"consecutive_image_failures": failures}))
            session.add(DirectorChildRun(id="child", **owner, workflow_id="workflow", task_id="task",
                kind=kind, title=kind, attempt=attempt, max_attempts=2, status=service.DirectorChildStatus.RUNNING))
            await session.commit()
        retried, stopped = [], []
        async def retry(session, workflow, child, task):
            retried.append(task.id)
            return None, None
        async def stop(session, workflow, **kwargs):
            stopped.append(kwargs["failure_reason"])
            await session.commit()
        async def dispatch(session, queued):
            await session.commit()
        monkeypatch.setattr(service, "_retry_child", retry)
        monkeypatch.setattr(service, "stop_automatic_workflow", stop)
        monkeypatch.setattr(service, "_commit_and_dispatch", dispatch)
        try:
            await service._advance_director_task_terminal("task")
            await service._advance_director_task_terminal("task")
            assert bool(stopped) == stops and len(stopped) <= 1
            assert len(retried) == (0 if stops else 1)
            async with sessions() as session:
                workflow = await session.get(DirectorWorkflowRun, "workflow")
                assert workflow.context_snapshot["consecutive_image_failures"] == failures + (kind == "asset_image")
        finally:
            await engine.dispose()
    asyncio.run(run())
