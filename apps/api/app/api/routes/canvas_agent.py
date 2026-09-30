"""Authenticated same-origin HTTP/SSE bridge to the local Codex canvas service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.api.deps import get_current_user, require_admin
from app.core.config import get_settings
from app.db.models import User, UserRole
from app.db.session import SessionLocal
from app.services.canvas_agent import existing_connection, is_alive, mcp_config, start_agent

router = APIRouter(prefix="/canvas/agent", tags=["canvas-agent"])


def bridge_token(user: User) -> str:
    settings = get_settings()
    return jwt.encode(
        {
            "sub": user.id,
            "tenant_id": user.tenant_id,
            "type": "canvas_agent",
            "iss": settings.jwt_issuer,
            "aud": "cineforge-canvas-agent",
            "exp": datetime.now(UTC) + timedelta(minutes=settings.access_token_minutes),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


async def bridge_user(request: Request) -> User:
    # EventSource and media elements cannot set Authorization headers. Their
    # token is scoped to this bridge and cannot authenticate other platform APIs.
    token = request.query_params.get("token", "")
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience="cineforge-canvas-agent",
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "tenant_id", "type", "exp"]},
        )
        if payload["type"] != "canvas_agent":
            raise jwt.InvalidTokenError()
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, "画布 Agent 连接已过期，请重新连接") from exc
    async with SessionLocal() as db:
        user = await db.get(User, payload["sub"])
        if not user or not user.is_active or user.tenant_id != payload["tenant_id"]:
            raise HTTPException(401, "账号登录状态无效")
        if user.role != UserRole.ADMIN:
            raise HTTPException(403, "服务器本机 Codex 仅供管理员连接；可连接你电脑上的 Agent")
        return user


@router.post("/start")
async def start(user: User = Depends(require_admin)):
    try:
        connection = await start_agent(user.tenant_id, user.id)
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "url": get_settings().api_prefix + "/canvas/agent",
        "token": bridge_token(user),
        "mcp": mcp_config(connection),
    }


@router.get("/connection")
async def connection_status(user: User = Depends(get_current_user)):
    if user.role != UserRole.ADMIN:
        return {"available": False, "running": False}
    connection = existing_connection(user.tenant_id, user.id)
    return {
        "available": True,
        "running": bool(connection and await is_alive(connection)),
        "mcp": mcp_config(connection) if connection else None,
    }


@router.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy(path: str, request: Request, user: User = Depends(bridge_user)):
    connection = existing_connection(user.tenant_id, user.id)
    if not connection:
        raise HTTPException(409, "请先启动本地 Canvas Agent")
    query = [(key, value) for key, value in request.query_params.multi_items() if key != "token"]
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=6), trust_env=False)
    upstream_request = client.build_request(
        request.method,
        f"{connection.url}/{path}",
        params=query,
        content=await request.body(),
        headers={
            "x-canvas-agent-token": connection.token,
            "Content-Type": request.headers.get("content-type", "application/json"),
        },
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(503, "本地 Canvas Agent 已断开，请重新启动连接") from exc
    headers = {
        "Content-Type": upstream.headers.get("content-type", "application/json"),
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    if "text/event-stream" in headers["Content-Type"]:

        async def events():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(events(), status_code=upstream.status_code, headers=headers)
    try:
        return Response(await upstream.aread(), status_code=upstream.status_code, headers=headers)
    finally:
        await upstream.aclose()
        await client.aclose()
