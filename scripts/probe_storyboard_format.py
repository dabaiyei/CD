"""One paid formatting-only probe of a saved shot; never changes project/task data."""
import asyncio
import json
import sys
import time
from uuid import uuid4

from app.core.config import get_settings
from app.core.security import SecretBox
from app.db.models import AIModel, AITask, Provider
from app.db.session import SessionLocal, engine
from app.services.agent_runtime import AgentRuntimeClient, AgentRuntimeRequest
from app.services.storyboard_generation import decode_shots, storyboard_format_request
from app.services.storyboard_review import run_review_attempt


async def main(task_id):
    async with SessionLocal() as db:
        task = await db.get(AITask, task_id)
        if not task:
            raise ValueError("Task not found")
        model = await db.get(AIModel, task.model_id)
        provider = await db.get(Provider, model.provider_id)
        state = task.request_payload["storyboard_generation"]["state"]
        row = next(iter(state["valid"].values()))
        capabilities = model.capabilities or {}
        probe_id = str(uuid4())
        request = AgentRuntimeRequest(
            tenant_id=task.tenant_id, project_id=task.project_id, task_id=probe_id, session_id=probe_id,
            prompt="", system_prompt="", prompt_versions={}, skill_versions={}, skills=[], memory_context=[],
            model_binding={"provider": provider.code, "model": model.model_id,
                "base_url": provider.base_url, "api_key": SecretBox().decrypt(provider.encrypted_api_key),
                "extra_headers": provider.extra_headers or {},
                "api_mode": capabilities.get("agent_api_mode", "chat_completions"),
                "reasoning_effort": "high", "max_tokens": capabilities.get("max_tokens")},
        )
    # Remove outer JSON closers, leaving the complete shot intact. This tests
    # format repair without asking the model to invent absent story content.
    raw = '{"shots":[' + json.dumps(row, ensure_ascii=False)
    request = storyboard_format_request(request, raw, '{"shots":[完整原镜头]}', [])
    settings = get_settings()
    client = AgentRuntimeClient(settings.agent_runtime_url, settings.agent_runtime_internal_token)

    async def progress(message):
        print(message, flush=True)

    started = time.monotonic()
    result = await run_review_attempt(client, request, progress, "正在修复格式探针")
    parsed = decode_shots(result.final_response)
    assert parsed == [row], "Format correction changed or lost saved shot fields"
    print(json.dumps({"elapsed_seconds": round(time.monotonic() - started, 1),
        "finish_reason": result.finish_reason, "tool_mode": request.tool_mode,
        "shots_unchanged": True, "model_calls": sum(e.get("type") == "MODEL_CALL_START" for e in result.events),
        "tool_calls": sum(e.get("type") == "TOOL_CALL_START" for e in result.events)}, ensure_ascii=False))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
