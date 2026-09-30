"""Single-pass Responses transport for self-contained ephemeral text tasks."""
from contextlib import suppress

from openai import AsyncOpenAI

from runtime import CONTRACT_VERSION
from runtime.contracts import AgentRunResponse, ExecutionManifest
from runtime.response_stream import IncompleteModelResponse, ResponseToolStream


def eligible(request):
    return (request.tool_mode == "none" and request.state_mode == "ephemeral"
        and request.model_binding.api_mode == "responses"
        and not any((request.project_files, request.skills, request.memory_context,
                     request.attachments, request.documents, request.recent_messages,
                     request.conversation_summary)))


async def run(request, timeout, on_event=None):
    binding = request.model_binding
    events, text = [], []
    async def emit(event):
        events.append({k: v for k, v in event.items() if k != "delta"})
        if on_event:
            with suppress(Exception):
                await on_event(event)
    parameters = {}
    if binding.reasoning_effort:
        parameters["reasoning"] = {"effort": binding.reasoning_effort}
    if binding.max_tokens is not None:
        parameters["max_output_tokens"] = binding.max_tokens
    await emit({"type": "MODEL_CALL_START"})
    complete = False
    async with AsyncOpenAI(api_key=binding.api_key.get_secret_value(),
            base_url=str(binding.base_url).rstrip("/") if binding.base_url else None,
            default_headers=binding.extra_headers or None, timeout=timeout, max_retries=0) as client:
        stream = await client.responses.create(model=binding.model, stream=True,
            input=[{"role": "system", "content": request.system_prompt},
                   {"role": "user", "content": request.prompt}], **parameters)
        async with ResponseToolStream(stream) as normalized:
            async for event in normalized:
                if event.type == "response.output_text.delta":
                    text.append(event.delta)
                    await emit({"type": "TEXT_BLOCK_DELTA", "delta": event.delta})
                elif event.type == "response.completed":
                    if any(getattr(item, "type", None) == "function_call"
                           for item in (event.response.output or [])):
                        raise IncompleteModelResponse("无工具任务返回了工具调用，未返回可用正文")
                    complete = True
    if not complete or not "".join(text).strip():
        raise IncompleteModelResponse("模型未返回正文或响应未完整结束；请仅重试本次请求")
    await emit({"type": "MODEL_CALL_END"})
    return AgentRunResponse(session_id=request.session_id, final_response="".join(text),
        finish_reason="completed", events=events,
        manifest=ExecutionManifest(contract_version=CONTRACT_VERSION, provider=binding.provider,
            model=binding.model, prompt_versions=request.prompt_versions, skill_versions=request.skill_versions))
