import asyncio

import httpx
import pytest

from app.services.agent_runtime import AgentRuntimeRequest
from app.services.generation_parallel import ParallelRuntime, bounded_each, task_write_lock


def request():
    return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="parent", session_id="session",
        prompt="test", system_prompt="", model_binding={}, prompt_versions={}, skill_versions={},
        skills=[], memory_context=[])


def test_bounded_runtime_isolates_workspaces_and_drains_on_cancel():
    async def scenario():
        entered = asyncio.Event()
        never = asyncio.Event()
        requests, active = [], set()

        class Runtime:
            async def run(self, req):
                requests.append(req)
                active.add(req.task_id)
                if len(active) == 3:
                    entered.set()
                try:
                    await never.wait()
                finally:
                    active.remove(req.task_id)

        runtime = ParallelRuntime(Runtime, 3)
        parent = asyncio.create_task(bounded_each(range(100), lambda _: runtime.run(request()), 3))
        await asyncio.wait_for(entered.wait(), 2)
        assert len(requests) == len(active) == 3
        assert len({r.session_id for r in requests}) == 3
        assert all(r.task_id != "parent" and r.state_mode == "ephemeral" for r in requests)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        assert not active and runtime.active == 0

    asyncio.run(scenario())


def test_transient_error_retries_and_reduces_concurrency():
    async def scenario():
        calls = []

        class Runtime:
            async def run(self, req):
                calls.append(req)
                if len(calls) == 1:
                    response = httpx.Response(429, request=httpx.Request("POST", "https://example.org"))
                    raise httpx.HTTPStatusError("busy", request=response.request, response=response)
                return "ok"

        runtime = ParallelRuntime(Runtime, 3)
        assert await runtime.run(request()) == "ok"
        assert len(calls) == 2 and runtime.limit == 1 and runtime.active == 0
        assert calls[0].task_id != calls[1].task_id
        runtime = ParallelRuntime(Runtime, 3)
        runtime.invalid_output()
        assert runtime.limit == 3
        runtime.invalid_output()
        assert runtime.limit == 1

    asyncio.run(scenario())


def test_streamed_review_does_not_multiply_retry_budget():
    # A real callback selects streaming mode.
    async def callback(event):
        pass
    async def exercise():
        calls = []
        class Runtime:
            async def run_stream(self, req, on_event):
                calls.append(req)
                await on_event({"type": "MODEL_CALL_START"})
                raise httpx.ReadTimeout("slow provider")
        runtime = ParallelRuntime(Runtime, 4)
        with pytest.raises(httpx.ReadTimeout):
            await runtime.run_stream(request(), callback)
        assert len(calls) == 1 and runtime.active == 0
    asyncio.run(exercise())


def test_stream_cancel_releases_slot_and_cancels_underlying_call():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Runtime:
            async def run_stream(self, req, on_event):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        async def callback(event):
            pass

        runtime = ParallelRuntime(Runtime, 4)
        pending = asyncio.create_task(runtime.run_stream(request(), callback))
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert cancelled.is_set() and runtime.active == 0

    asyncio.run(scenario())


def test_fatal_worker_error_cancels_siblings():
    async def scenario():
        ready = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(index):
            if index == 0:
                await ready.wait()
                raise RuntimeError("任务已停止")
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with pytest.raises(RuntimeError, match="已停止"):
            await asyncio.wait_for(bounded_each(range(20), handler, 2), 2)
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_same_task_checkpoints_do_not_overwrite_each_other():
    async def scenario():
        checkpoint = {}

        async def write(index):
            nonlocal checkpoint
            async with task_write_lock("parent"):
                updated = {**checkpoint, str(index): index}
                await asyncio.sleep(0)
                checkpoint = updated

        await bounded_each(range(20), write, 3)
        assert len(checkpoint) == 20

    asyncio.run(scenario())
