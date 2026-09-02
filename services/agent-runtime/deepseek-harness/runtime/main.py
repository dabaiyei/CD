from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from runtime.adapter import DeepSeekHarnessAdapter, RuntimeAdapter
from runtime.config import Settings, get_settings
from runtime.context import InvalidProjectFileSnapshot, InvalidSkillSnapshot
from runtime.contracts import AgentRunRequest, AgentRunResponse, HealthResponse

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.absolute_data_root.mkdir(parents=True, exist_ok=True)
    app.state.adapter = DeepSeekHarnessAdapter(settings)
    app.state.run_slots = asyncio.Semaphore(settings.max_concurrent_runs)
    yield


app = FastAPI(
    title="CineForge Agent Runtime",
    version="0.2.0",
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
    return HealthResponse(harness_available=adapter.available)


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
    if not adapter.available:
        raise HTTPException(status_code=503, detail="DeepSeek Harness runtime is unavailable")
    try:
        async with request.app.state.run_slots:
            return await run_in_threadpool(adapter.run, payload)
    except (InvalidSkillSnapshot, InvalidProjectFileSnapshot) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Agent Runtime execution failed") from exc


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
    return await run_agent(payload, request, adapter)
