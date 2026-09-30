from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.replica_production import owned, store
from app.core.config import get_settings
from app.db.models import User
from app.db.session import SessionLocal, get_session
from app.services import replica_production, replica_studio

router = APIRouter(prefix="/video-replicas", tags=["video-replicas"])


@asynccontextmanager
async def mutation_lock(studio, mutation):
    # Independent JS, fonts and media requests must be able to load concurrently.
    # Only editor writes need serialization for source/revision consistency.
    if mutation:
        async with studio.lock:
            yield
    else:
        yield


@router.post("/{task_id}/studio")
async def start(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    task, value = await owned(db, task_id, user)
    try:
        studio = await replica_studio.start(task, user, value)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(422, str(exc) or "制作编辑器启动超时") from exc
    prefix = f"{get_settings().api_prefix}/video-replicas/studio/{studio.id}"
    return {"id": studio.id, "url": prefix + "/?ticket=" + studio.ticket}


@router.post("/{task_id}/studio/{session_id}/save")
async def save(
    task_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    studio = replica_studio.sessions.get(session_id)
    if not studio or (studio.tenant_id, studio.user_id, studio.task_id) != (user.tenant_id, user.id, task_id):
        raise HTTPException(404, "制作编辑器已关闭或不属于此工程")
    task, _ = await owned(db, task_id, user)
    async with studio.lock:
        try:
            value = {**studio.production, "files": replica_studio.edited_files(studio)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        value = await store(db, task, value, studio.production["revision"])
        studio.production = value
        studio.touched = time.monotonic()
        return value


@router.delete("/{task_id}/studio/{session_id}")
async def close(task_id: str, session_id: str, user: User = Depends(get_current_user)):
    studio = replica_studio.sessions.get(session_id)
    if studio and (studio.tenant_id, studio.user_id, studio.task_id) == (user.tenant_id, user.id, task_id):
        await replica_studio.close(studio)
    return {"closed": True}


def authorized(session_id, cookies):
    studio = replica_studio.sessions.get(session_id)
    if not studio or not secrets.compare_digest(cookies.get("hypit_session", ""), studio.ticket):
        raise HTTPException(401, "制作编辑器会话已过期，请重新打开")
    studio.touched = time.monotonic()
    return studio


@router.websocket("/studio/{session_id}/socket")
async def socket_proxy(websocket: WebSocket, session_id: str):
    from websockets.asyncio.client import connect

    try:
        studio = authorized(session_id, websocket.cookies)
    except HTTPException:
        await websocket.close(code=4401)
        return
    origin = websocket.headers.get("origin", "")
    if origin and origin.split("://", 1)[-1] != websocket.headers.get("host"):
        await websocket.close(code=4403)
        return
    protocols = [
        p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()
    ]
    try:
        async with connect(
            studio.origin.replace("http:", "ws:") + "/?" + websocket.url.query,
            subprotocols=protocols,
            origin=studio.origin,
            proxy=None,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def inward():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(
                        message.get("text") if message.get("text") is not None else message["bytes"]
                    )

            async def outward():
                async for message in upstream:
                    if isinstance(message, str):
                        prefix = f"{get_settings().api_prefix}/video-replicas/studio/{session_id}"
                        await websocket.send_text(replica_studio.rewrite(message, prefix))
                    else:
                        await websocket.send_bytes(message)

            tasks = [asyncio.create_task(inward()), asyncio.create_task(outward())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        with suppress(RuntimeError):
            await websocket.close(code=1011)


@router.api_route("/studio/{session_id}/{path:path}", methods=["GET", "HEAD", "POST", "PUT"])
async def proxy(session_id: str, path: str, request: Request):
    prefix = request.url.path.removesuffix(path).rstrip("/")
    ticket = request.query_params.get("ticket")
    studio = replica_studio.sessions.get(session_id)
    if not path and ticket and studio and secrets.compare_digest(ticket, studio.ticket):
        response = RedirectResponse(prefix + "/", status_code=303)
        response.set_cookie(
            "hypit_session",
            studio.ticket,
            path=prefix + "/",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=1800,
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    studio = authorized(session_id, request.cookies)
    if not replica_studio.permitted_path(path, studio):
        raise HTTPException(403, "不可读取工程之外的文件")
    mutation = request.method in {"POST", "PUT"}
    if mutation:
        origin = request.headers.get("origin", "")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "不可跨站修改制作工程")
        if path not in {"__studio/source", "__studio/mutation", "__studio/feedback"}:
            raise HTTPException(403, "此操作未接入；请使用工作台的导出操作")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "单次工程编辑过大")
    headers = {"accept-encoding": "identity", "origin": studio.origin}
    for name in ("content-type", "range"):
        if name in request.headers:
            headers[name] = request.headers[name]
    async with mutation_lock(studio, mutation):
        before = replica_studio.edited_files(studio) if mutation else None
        if path == "__studio/source" and mutation:
            try:
                data = json.loads(body)
                name = data.get("path", "composition.svml")
                if name not in replica_production.SOURCE_FILES or not isinstance(data.get("text"), str):
                    raise ValueError("只能修改当前工程源文件")
                replica_production.validate_sources(
                    {**before, name: data["text"]}, studio.production["assets"]
                )
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.request(
                request.method,
                studio.origin + "/" + path,
                params=request.query_params,
                headers=headers,
                content=bytes(body),
            )
        if mutation:
            try:
                files = replica_studio.edited_files(studio)
            except ValueError as exc:
                for name in replica_production.SOURCE_FILES:
                    (studio.root / name).write_text(before[name], encoding="utf-8")
                raise HTTPException(422, str(exc)) from exc
            if response.is_success:
                # Persist native edits immediately, including timestamped comments.
                # The browser tab is not the sole owner of the production's state.
                async with SessionLocal() as db:
                    user = await db.get(User, studio.user_id)
                    if not user or not user.is_active or user.tenant_id != studio.tenant_id:
                        raise HTTPException(401, "账号不可用")
                    task, _ = await owned(db, studio.task_id, user)
                    studio.production = await store(
                        db, task, {**studio.production, "files": files}, studio.production["revision"]
                    )
    content_type = response.headers.get("content-type", "application/octet-stream")
    content = response.content
    if any(t in content_type for t in ("javascript", "text/html", "text/css", "application/json")):
        content = replica_studio.rewrite(response.text, prefix, html="text/html" in content_type).encode()
    outgoing = {"content-type": content_type, "cache-control": "no-store", "referrer-policy": "no-referrer"}
    for name in ("content-range", "accept-ranges"):
        if name in response.headers:
            outgoing[name] = response.headers[name]
    return Response(content, status_code=response.status_code, headers=outgoing)
