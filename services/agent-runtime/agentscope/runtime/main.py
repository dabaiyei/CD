from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from runtime.adapter import AgentScopeAdapter, RuntimeAdapter
from runtime.config import Settings, get_settings
from runtime.context import InvalidProjectFileSnapshot, InvalidSkillSnapshot
from runtime.contracts import SAFE_ID, AgentRunRequest, AgentRunResponse, HealthResponse

settings = get_settings()
logger = logging.getLogger(__name__)


def _runtime_error_message(exc: Exception) -> str:
    """Return an actionable error without leaking upstream response bodies or credentials."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code in {401, 403}:
            return f"模型供应商鉴权失败 (HTTP {status_code})"
        if status_code == 404:
            return "模型或 Chat Completions 接口不存在 (HTTP 404)"
        if status_code == 429:
            return "模型供应商请求过于频繁或额度不足 (HTTP 429)"
        if status_code in {400, 405, 409, 415, 422}:
            detail = str(exc).lower()
            if (
                status_code == 400
                and "json_parse_error" in detail
                and "responseinput" in detail
            ):
                return "模型的 Responses 接口连续无法解析请求，系统已自动重试 (HTTP 400)"
            return f"模型与所选 OpenAI 接口或工具调用格式不兼容 (HTTP {status_code})"
        if status_code >= 500:
            return f"模型供应商服务异常 (HTTP {status_code})"

    # The native xAI SDK uses gRPC and exposes INVALID_ARGUMENT instead of an
    # HTTP status. Keep the provider detail useful without echoing its body.
    grpc_code = getattr(exc, "code", None)
    if callable(grpc_code):
        try:
            code_name = str(grpc_code()).rsplit(".", 1)[-1].upper()
        except Exception:
            code_name = ""
        if code_name == "INVALID_ARGUMENT":
            return "Grok 请求参数或工具调用格式不兼容，请检查模型名称与 xAI 官方地址"
        if code_name in {"UNAUTHENTICATED", "PERMISSION_DENIED"}:
            return "Grok API Key 无效或没有模型权限"
        if code_name == "RESOURCE_EXHAUSTED":
            return "Grok 额度不足或请求过于频繁"
        if code_name in {"UNAVAILABLE", "DEADLINE_EXCEEDED"}:
            return "Grok 服务暂时不可用或响应超时"

    exception_name = type(exc).__name__.lower()
    if "timeout" in exception_name:
        return "模型供应商响应超时"
    if "connect" in exception_name:
        return "无法连接模型供应商"
    return f"Agent Runtime 执行失败 ({type(exc).__name__})"


def _log_runtime_exception(payload: AgentRunRequest, exc: Exception) -> None:
    logger.exception(
        "Agent Runtime execution failed task_id=%s session_id=%s provider=%s model=%s "
        "exception_type=%s",
        payload.task_id,
        payload.session_id,
        payload.model_binding.provider,
        payload.model_binding.model,
        type(exc).__name__,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.absolute_data_root.mkdir(parents=True, exist_ok=True)
    app.state.adapter = AgentScopeAdapter(settings)
    app.state.run_slots = asyncio.Semaphore(settings.max_concurrent_runs)
    yield


app = FastAPI(
    title="CineForge AgentScope Runtime",
    version="0.4.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


def require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
    runtime_settings: Settings = Depends(get_settings),
) -> None:
    if x_internal_token is None or not hmac.compare_digest(x_internal_token, runtime_settings.internal_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal token")


def get_adapter(request: Request) -> RuntimeAdapter:
    return request.app.state.adapter


@app.get("/health", response_model=HealthResponse)
async def health(adapter: RuntimeAdapter = Depends(get_adapter)) -> HealthResponse:
    return HealthResponse(agentscope_available=adapter.available)


async def _run(payload: AgentRunRequest, request: Request, adapter: RuntimeAdapter) -> AgentRunResponse:
    if not adapter.available:
        raise HTTPException(status_code=503, detail="AgentScope runtime is unavailable")
    try:
        async with request.app.state.run_slots:
            return await adapter.run(payload)
    except (InvalidSkillSnapshot, InvalidProjectFileSnapshot) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        _log_runtime_exception(payload, exc)
        raise HTTPException(status_code=502, detail=_runtime_error_message(exc)) from exc


@app.post(
    "/internal/v1/runs",
    response_model=AgentRunResponse,
    dependencies=[Depends(require_internal_token)],
)
async def run_agent(
    payload: AgentRunRequest,
    request: Request,
    adapter: RuntimeAdapter = Depends(get_adapter),
) -> AgentRunResponse:
    return await _run(payload, request, adapter)


@app.post(
    "/internal/v2/runs",
    response_model=AgentRunResponse,
    dependencies=[Depends(require_internal_token)],
)
async def run_agent_v2(
    payload: AgentRunRequest,
    request: Request,
    adapter: RuntimeAdapter = Depends(get_adapter),
) -> AgentRunResponse:
    if payload.contract_version != "v2":
        raise HTTPException(status_code=422, detail="v2 route requires contract_version v2")
    return await _run(payload, request, adapter)


@app.delete(
    "/internal/v2/sessions/{tenant_id}/{project_id}/{session_id}",
    dependencies=[Depends(require_internal_token)],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_agent_session_state(
    tenant_id: str,
    project_id: str,
    session_id: str,
    adapter: RuntimeAdapter = Depends(get_adapter),
) -> None:
    if any(
        not SAFE_ID.fullmatch(value) or value in {".", ".."}
        for value in (tenant_id, project_id, session_id)
    ):
        raise HTTPException(status_code=422, detail="invalid session scope")
    delete_state = getattr(adapter, "delete_session_state", None)
    if not callable(delete_state):
        raise HTTPException(status_code=501, detail="runtime does not support state deletion")
    await asyncio.to_thread(delete_state, tenant_id, project_id, session_id)


@app.post(
    "/internal/v2/runs/stream",
    dependencies=[Depends(require_internal_token)],
)
async def stream_agent_v2(
    payload: AgentRunRequest,
    request: Request,
    adapter: RuntimeAdapter = Depends(get_adapter),
) -> StreamingResponse:
    if payload.contract_version != "v2":
        raise HTTPException(status_code=422, detail="v2 route requires contract_version v2")
    if not adapter.available:
        raise HTTPException(status_code=503, detail="AgentScope runtime is unavailable")

    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def emit(event: dict[str, object]) -> None:
            await queue.put({"type": "event", "event": event})

        async def execute() -> None:
            try:
                async with request.app.state.run_slots:
                    result = await adapter.run(payload, emit)
                await queue.put({"type": "result", "result": result.model_dump(mode="json")})
            except (InvalidSkillSnapshot, InvalidProjectFileSnapshot) as exc:
                await queue.put({"type": "error", "message": str(exc)})
            except Exception as exc:
                _log_runtime_exception(payload, exc)
                await queue.put({"type": "error", "message": _runtime_error_message(exc)})

        runner = asyncio.create_task(execute())
        try:
            while not await request.is_disconnected():
                item = await queue.get()
                yield json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                if item["type"] in {"result", "error"}:
                    break
        finally:
            if not runner.done():
                runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
