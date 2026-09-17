import asyncio

from fastapi import HTTPException

from app import worker
from app.services import director_orchestration


def test_broken_recovery_does_not_skip_other_recovery_steps(monkeypatch):
    calls = []

    async def stale():
        calls.append("stale")

    async def automatic():
        calls.append("automatic")
        raise HTTPException(status_code=402, detail="积分不足")

    async def reviews():
        calls.append("reviews")

    monkeypatch.setattr(worker, "recover_stale_tasks", stale)
    monkeypatch.setattr(director_orchestration, "recover_automatic_workflows", automatic)
    monkeypatch.setattr(director_orchestration, "recover_orphaned_agent_script_reviews", reviews)
    asyncio.run(worker.recover_pending_workflows())
    assert calls == ["stale", "automatic", "reviews"]
