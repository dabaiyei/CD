import asyncio

import pytest

from app.services import video_replica as replica


def test_one_failure_does_not_block_later_clips_and_resume_only_missing(monkeypatch):
    monkeypatch.setattr(replica, "CLIP_RETRY_DELAYS", (0, 0))
    results, state, calls, events = {}, {}, [], []
    failing = True

    async def generate(index, shot):
        calls.append(index)
        if index == 1 and failing:
            raise RuntimeError("provider temporary error")
        results[str(index)]["key"] = f"clip-{index}"

    async def progress(percent, message):
        events.append(message)

    with pytest.raises(RuntimeError, match="已保存 2/3 段"):
        asyncio.run(replica.complete_clips("task", [0, 1, 2], results, state, generate, progress))
    assert calls == [0, 1, 1, 1, 2]
    assert results["1"]["status"] == "failed"
    assert "error" in results["1"]
    failing = False
    asyncio.run(replica.complete_clips("task", [0, 1, 2], results, state, generate, progress))
    assert calls == [0, 1, 1, 1, 2, 1]
    assert results["1"]["status"] == "succeeded"
    assert "error" not in results["1"]


def test_temporary_error_recovers_automatically(monkeypatch):
    monkeypatch.setattr(replica, "CLIP_RETRY_DELAYS", (0, 0))
    results, calls = {}, []

    async def generate(index, shot):
        calls.append(index)
        if len(calls) == 1:
            results["0"]["provider_job_id"] = "existing-job"
            raise RuntimeError("poll failed")
        assert results["0"]["provider_job_id"] == "existing-job"
        results["0"]["key"] = "finished"

    async def progress(*args):
        pass

    asyncio.run(replica.complete_clips("task", [0], results, {}, generate, progress))
    assert calls == [0, 0]


def test_cancellation_is_not_retried(monkeypatch):
    monkeypatch.setattr(replica, "CLIP_RETRY_DELAYS", (0, 0))
    calls = []

    async def generate(index, shot):
        calls.append(index)
        raise asyncio.CancelledError()

    async def progress(*args):
        pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(replica.complete_clips("task", [0, 1], {}, {}, generate, progress))
    assert calls == [0]


def test_lost_lease_stops_before_retry_or_next_clip(monkeypatch):
    monkeypatch.setattr(replica, "CLIP_RETRY_DELAYS", (0, 0))
    calls = []

    async def generate(index, shot):
        calls.append(index)
        raise RuntimeError("query failed")

    async def progress(*args):
        if calls:
            raise RuntimeError("task ownership lost")

    with pytest.raises(RuntimeError, match="ownership lost"):
        asyncio.run(replica.complete_clips("task", [0, 1], {}, {}, generate, progress))
    assert calls == [0]
