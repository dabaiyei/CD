from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.models import AITask, Notification, TaskStatus, User
from app.db.session import SessionLocal, get_session
from app.domain.schemas import NotificationPage
from app.services.task_queue import redis_client

router = APIRouter(prefix="/notifications", tags=["notifications"])
STREAM_HEARTBEAT_SECONDS = 15.0
STREAM_RETRY_MILLISECONDS = 3_000
POLLING_STREAM_INTERVAL_SECONDS = 0.5


def sse_message(data: dict[str, object], *, event: str | None = None) -> str:
    lines = [f"event: {event}"] if event else []
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


async def user_event_stream(user_id: str, request: Request) -> AsyncIterator[str]:
    yield f"retry: {STREAM_RETRY_MILLISECONDS}\n\n"
    client = redis_client()
    if client is None:
        yield sse_message({"type": "stream.ready", "transport": "polling"}, event="ready")
        snapshots: dict[str, dict[str, object]] = {}
        last_heartbeat = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            async with SessionLocal() as session:
                rows = (
                    await session.execute(
                        select(AITask.id, AITask.project_id, AITask.result_payload).where(
                            AITask.user_id == user_id,
                            AITask.task_type == "agent_chat_run",
                            AITask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
                        )
                    )
                ).all()
            for task_id, project_id, result_payload in rows:
                snapshot = (result_payload or {}).get("agent_stream")
                if not isinstance(snapshot, dict) or snapshot == snapshots.get(task_id):
                    continue
                snapshots[task_id] = snapshot
                yield sse_message(
                    {
                        "type": "agent.stream",
                        "task_id": task_id,
                        "project_id": project_id,
                        "session_id": snapshot.get("session_id"),
                        "event": "snapshot",
                        "text": snapshot.get("text", ""),
                        "phase": snapshot.get("phase", "thinking"),
                        "tool_name": snapshot.get("tool_name", ""),
                        "tool_state": snapshot.get("tool_state", ""),
                        "execution_steps": snapshot.get("execution_steps", []),
                        "created_at": snapshot.get("updated_at"),
                    },
                    event="activity",
                )
            active_ids = {row.id for row in rows}
            snapshots = {key: value for key, value in snapshots.items() if key in active_ids}
            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(POLLING_STREAM_INTERVAL_SECONDS)
        return

    channel = f"{get_settings().task_event_channel}:{user_id}"
    while not await request.is_disconnected():
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(channel)
            yield sse_message({"type": "stream.ready", "transport": "redis"}, event="ready")
            while not await request.is_disconnected():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=STREAM_HEARTBEAT_SECONDS,
                )
                if message and isinstance(message.get("data"), str):
                    yield f"event: activity\ndata: {message['data']}\n\n"
                else:
                    yield ": heartbeat\n\n"
        except RedisError:
            yield sse_message({"type": "stream.degraded", "transport": "polling"}, event="degraded")
            await asyncio.sleep(STREAM_RETRY_MILLISECONDS / 1_000)
        finally:
            await pubsub.aclose()


@router.get("", response_model=NotificationPage)
async def list_notifications(
    unread_only: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> NotificationPage:
    filters = [Notification.tenant_id == user.tenant_id, Notification.user_id == user.id]
    query = select(Notification).where(*filters)
    if unread_only:
        query = query.where(Notification.is_read.is_(False))
    items = list(
        (await session.scalars(query.order_by(Notification.created_at.desc()).limit(limit))).all()
    )
    unread_count = await session.scalar(
        select(func.count(Notification.id)).where(*filters, Notification.is_read.is_(False))
    )
    return NotificationPage(items=items, unread_count=unread_count or 0)


@router.get("/stream")
async def stream_notifications(
    request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    return StreamingResponse(
        user_event_stream(user.id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.patch("/{notification_id}/read", status_code=204)
async def read_notification(
    notification_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    notification = await session.get(Notification, notification_id)
    if (
        notification is None
        or notification.tenant_id != user.tenant_id
        or notification.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="通知不存在")
    notification.is_read = True
    await session.commit()


@router.post("/read-all", status_code=204)
async def read_all_notifications(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await session.execute(
        update(Notification)
        .where(
            Notification.tenant_id == user.tenant_id,
            Notification.user_id == user.id,
            Notification.is_read.is_(False),
        )
        .values(is_read=True)
    )
    await session.commit()
