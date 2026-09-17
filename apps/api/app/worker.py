from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.migration_guard import assert_database_current
from app.services.task_queue import close_redis, dequeue_task
from app.services.task_worker import process_next_task, process_task, recover_stale_tasks
from app.services.site_maintenance import activity

logger = logging.getLogger(__name__)


def database_target(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        return f"sqlite:{Path(url.database or '').resolve()}"
    return url.render_as_string(hide_password=True)


async def recover_stale_tasks_periodically() -> None:
    interval = max(5.0, get_settings().task_recovery_interval_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            async with activity() as allowed:
                if allowed:
                    await recover_stale_tasks()
        except Exception:
            logger.exception("Periodic stale task recovery failed")


async def recover_pending_workflows() -> None:
    from app.services.director_orchestration import (
        recover_automatic_workflows,
        recover_orphaned_agent_script_reviews,
    )

    for recover in (recover_stale_tasks, recover_automatic_workflows, recover_orphaned_agent_script_reviews):
        try:
            async with activity() as allowed:
                if allowed:
                    await recover()
        except Exception:
            logger.exception("%s failed; worker continues", recover.__name__)


async def worker_slot(slot: int) -> None:
    settings = get_settings()
    while True:
        try:
            async with activity() as allowed:
                if allowed:
                    task_id = await dequeue_task(settings.task_poll_interval_seconds)
                    if task_id and await process_task(task_id):
                        continue
                    processed = await process_next_task()
                    if processed is not None:
                        continue
            await asyncio.sleep(settings.task_poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker slot %s failed while polling; retrying", slot)
            await asyncio.sleep(settings.task_poll_interval_seconds)


async def run_worker() -> None:
    settings = get_settings()
    logger.info(
        "Starting worker with database=%s concurrency=%s agent_chat_idle_timeout=%ss "
        "task_lease_timeout=%ss recovery_interval=%ss",
        database_target(settings.database_url),
        settings.worker_concurrency,
        settings.agent_chat_task_timeout_seconds,
        settings.task_lease_timeout_seconds,
        settings.task_recovery_interval_seconds,
    )
    await assert_database_current()
    await recover_pending_workflows()
    recovery = asyncio.create_task(recover_stale_tasks_periodically())
    slots = [
        asyncio.create_task(worker_slot(index + 1), name=f"worker-slot-{index + 1}")
        for index in range(settings.worker_concurrency)
    ]
    try:
        await asyncio.gather(*slots)
    finally:
        for slot in slots:
            slot.cancel()
        recovery.cancel()
        for slot in slots:
            with suppress(asyncio.CancelledError):
                await slot
        with suppress(asyncio.CancelledError):
            await recovery
        await close_redis()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(run_worker())
