from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AITask, Project, TaskEvent, TaskStatus, User
from app.services.billing import debit_task_cost
from app.services.readiness import require_core_models_ready
from app.services.task_events import record_task_event

ACTIVE_TASK_STATUSES = {TaskStatus.QUEUED, TaskStatus.RUNNING}


async def serialize_project_task_submissions(
    session: AsyncSession,
    project_id: str,
) -> None:
    project = await session.scalar(
        select(Project.id).where(Project.id == project_id).with_for_update()
    )
    if project is None:
        raise RuntimeError("task project disappeared before submission")


async def active_tasks(
    session: AsyncSession,
    *,
    project_id: str,
    task_type: str,
) -> list[AITask]:
    await serialize_project_task_submissions(session, project_id)
    return list(
        (
            await session.scalars(
                select(AITask).where(
                    AITask.project_id == project_id,
                    AITask.task_type == task_type,
                    AITask.status.in_(ACTIVE_TASK_STATUSES),
                )
            )
        ).all()
    )


async def create_queued_task(
    session: AsyncSession,
    *,
    user: User,
    project_id: str | None,
    task_type: str,
    model_id: str | None,
    cost: Decimal,
    request_payload: dict,
    message: str,
) -> tuple[AITask, TaskEvent]:
    await require_core_models_ready(session, user.tenant_id)
    task = AITask(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        task_type=task_type,
        model_id=model_id,
        cost=cost,
        request_payload=request_payload,
    )
    session.add(task)
    await session.flush()
    task.idempotency_key = task.id
    await debit_task_cost(session, task, reason=message)
    event = record_task_event(
        session,
        task,
        status=TaskStatus.QUEUED,
        progress=0,
        message=f"{message}已进入队列",
    )
    return task, event
