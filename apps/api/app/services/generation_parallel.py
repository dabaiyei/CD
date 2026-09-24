"""Bounded model calls, provider-wide task reservations and short serial writes."""
from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable, Iterable
from uuid import uuid4

import httpx
from sqlalchemy import case, func, select, update

from app.db.models import AIModel, AITask, Provider, TaskStatus
from app.db.session import SessionLocal

_write_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def task_write_lock(task_id: str) -> asyncio.Lock:
    key = (asyncio.get_running_loop(), task_id)
    lock = _write_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _write_locks[key] = lock
    return lock


def running_slots():
    slots = AITask.request_payload["runtime_concurrency_slots"].as_integer()
    return func.coalesce(func.sum(case((slots > 1, slots), else_=1)), 0)


async def reserve_task_slots(task_id: str, desired: int) -> int:
    """Reserve extra slots on the same provider row used by the task claimer.

    A running task already owns one slot. Reservations live with its lease and
    cease counting when it stops; a reclaim always starts again with one slot.
    The no-op UPDATE also serializes competing workers on SQLite, whose
    SELECT FOR UPDATE otherwise provides no exclusion.
    """
    from app.services.task_worker import owned_task_for_update, owns_running_task

    async with SessionLocal() as session:
        provider_id = await session.scalar(select(AIModel.provider_id).join(
            AITask, AITask.model_id == AIModel.id
        ).where(AITask.id == task_id))
    if not provider_id:
        return 1
    async with task_write_lock(task_id), SessionLocal() as session:
        await session.execute(update(Provider).where(Provider.id == provider_id).values(
            updated_at=Provider.updated_at
        ))
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            raise RuntimeError("生成任务已停止或上下文已失效")
        provider = await session.get(Provider, provider_id)
        used = await session.scalar(select(running_slots()).select_from(AITask).join(
            AIModel, AIModel.id == AITask.model_id
        ).where(AIModel.provider_id == provider_id, AITask.status == TaskStatus.RUNNING))
        current = max(1, int(task.request_payload.get("runtime_concurrency_slots") or 1))
        available = max(1, int(provider.max_concurrency) - int(used or 0) + current)
        granted = max(1, min(desired, available))
        task.request_payload = {**task.request_payload, "runtime_concurrency_slots": granted}
        await session.commit()
        return granted


def task_stopped(error: BaseException) -> bool:
    return "已停止" in str(error) or "上下文已失效" in str(error)


async def bounded_each(items: Iterable, handler: Callable[..., Awaitable], limit: int) -> None:
    """Do not create one asyncio Task per shot in a very long chapter."""
    iterator = iter(items)

    async def consume():
        for item in iterator:
            await handler(item)

    workers = [asyncio.create_task(consume()) for _ in range(max(1, limit))]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


class ParallelRuntime:
    """A factory-compatible limiter shared by every model call in one task."""

    def __init__(self, factory, limit: int, *, task_id: str | None = None):
        self.factory = factory
        self.task_id = task_id
        self.limit = max(1, limit)
        self.active = 0
        self.condition = asyncio.Condition()
        self.invalid_outputs = 0

    def __call__(self):
        return self

    def invalid_output(self):
        self.invalid_outputs += 1
        if self.invalid_outputs >= 2:
            self.limit = 1

    async def run(self, request):
        return await self._run(request)

    async def run_stream(self, request, on_event):
        # The reviewer owns its retry budget. Do not multiply its three attempts
        # by another three hidden transport retries.
        return await self._run(request, on_event=on_event, attempts=1)

    async def _run(self, request, *, on_event=None, attempts=3):
        for attempt in range(attempts):
            async with self.condition:
                await self.condition.wait_for(lambda: self.active < self.limit)
                self.active += 1
            try:
                if self.task_id:
                    from app.services.task_worker import owned_task_for_update, owns_running_task

                    async with SessionLocal() as session:
                        current = await owned_task_for_update(session, self.task_id)
                        if not owns_running_task(current):
                            raise RuntimeError("生成任务已停止或上下文已失效")
                # Ephemeral runtime workspaces are keyed by task_id, not
                # session_id. Both must be unique to prevent parallel cleanup
                # or state writes from touching another shot's files.
                suffix = uuid4().hex
                isolated = request.model_copy(update={
                    "task_id": f"{request.task_id[:90]}-{suffix}",
                    "session_id": f"{request.session_id[:90]}-{suffix}",
                    "state_mode": "ephemeral",
                })
                runtime = self.factory()
                if on_event is not None and hasattr(runtime, "run_stream"):
                    return await runtime.run_stream(isolated, on_event)
                return await runtime.run(isolated)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                transient = isinstance(exc, httpx.TransportError) or status in {429, 502, 503, 504}
                transient = transient or any(token in str(exc).lower() for token in (
                    "http 429", "http 502", "http 503", "http 504", "rate limit", "timeout", "超时"
                ))
                if not transient or task_stopped(exc):
                    raise
                self.limit = 1
                if attempt == attempts - 1:
                    raise
            finally:
                async with self.condition:
                    self.active -= 1
                    self.condition.notify_all()
            await asyncio.sleep(2 ** attempt)
