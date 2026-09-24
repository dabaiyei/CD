"""One minimal provider tool-call probe; never prints credentials or arguments."""
import asyncio
import json
import sys
import hashlib
from uuid import uuid4
from collections import Counter

from openai import AsyncOpenAI

from app.core.security import SecretBox
from app.db.models import AITask, AIModel, Provider
from app.db.session import SessionLocal, engine


async def main():
    async with SessionLocal() as session:
        task = await session.get(AITask, sys.argv[1])
        model = await session.get(AIModel, task.model_id)
        provider = await session.get(Provider, model.provider_id)
        mode = (model.capabilities or {}).get("agent_api_mode", "chat_completions")
        print(json.dumps({"model": model.model_id, "mode": mode}), flush=True)
        client = AsyncOpenAI(api_key=SecretBox().decrypt(provider.encrypted_api_key),
                             base_url=provider.base_url, default_headers=provider.extra_headers or {},
                             timeout=90, max_retries=0)
        if "--runtime" in sys.argv:
            from app.services.agent_runtime import AgentRuntimeRequest, AgentRuntimeProjectFileSnapshot
            from app.services.task_worker import default_runtime_factory
            token = uuid4().hex
            content = json.dumps({"probe_token": token})
            request = AgentRuntimeRequest(tenant_id=task.tenant_id, project_id=task.project_id,
                task_id="probe-" + uuid4().hex, session_id="probe-" + uuid4().hex,
                prompt="Read project-files/probe/token.json using Read. Return JSON with probe_token from that file.",
                system_prompt="Read the file, then return the requested JSON. Do not stop at a plan.",
                model_binding={"provider": provider.code, "model": model.model_id, "base_url": provider.base_url,
                    "api_key": SecretBox().decrypt(provider.encrypted_api_key), "extra_headers": provider.extra_headers or {},
                    "api_mode": mode}, prompt_versions={}, skill_versions={}, skills=[], memory_context=[],
                tool_mode="retrieval", state_mode="ephemeral", project_files=[AgentRuntimeProjectFileSnapshot(
                    id="probe", name="token.json", kind="context", path="project-files/probe/token.json",
                    content=content, sha256=hashlib.sha256(content.encode()).hexdigest(), editable=False)])
            events = []
            async def record(event):
                if event.get("type") in {"TOOL_CALL_START", "TOOL_RESULT_END", "MODEL_CALL_END", "REPLY_END"}:
                    safe = {key: event.get(key) for key in ("type", "tool_call_name", "finished_reason", "state")}
                    events.append(safe)
                    print(json.dumps(safe), flush=True)
            response = await default_runtime_factory().run_stream(request, record)
            print(json.dumps({"finish": response.finish_reason, "matched": token in response.final_response,
                              "response": response.final_response[:1000], "events": events}), flush=True)
            await client.close()
            await engine.dispose()
            return
    tool = {"name": "Read", "description": "Read a local file", "parameters": {
        "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
    prompt = "Use the Read tool to read project-files/probe/token.txt. Do not guess its contents."
    counts = Counter()
    details = []
    async with client:
        if mode == "responses":
            stream = await client.responses.create(model=model.model_id, input=prompt,
                                                   tools=[{"type": "function", **tool}], stream=True)
            async for event in stream:
                counts[event.type] += 1
                item = getattr(event, "item", None)
                if item is not None:
                    details.append({"event": event.type, "type": getattr(item, "type", None),
                                    "id": getattr(item, "id", None), "call_id": getattr(item, "call_id", None),
                                    "name": getattr(item, "name", None),
                                    "argument_chars": len(getattr(item, "arguments", "") or "")})
                if event.type == "response.function_call_arguments.delta" and counts[event.type] == 1:
                    details.append({"argument_item_id": getattr(event, "item_id", None)})
                if event.type == "response.completed":
                    details.append({"final_types": [item.type for item in event.response.output]})
        else:
            stream = await client.chat.completions.create(model=model.model_id,
                messages=[{"role": "user", "content": prompt}],
                tools=[{"type": "function", "function": tool}], stream=True)
            async for chunk in stream:
                for choice in chunk.choices:
                    counts["tool_deltas"] += len(choice.delta.tool_calls or [])
                    counts["text_deltas"] += bool(choice.delta.content)
                    if choice.finish_reason:
                        details.append({"finish_reason": choice.finish_reason})
    print(json.dumps({"events": counts, "details": details}, ensure_ascii=False), flush=True)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
