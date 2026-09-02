from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings


@lru_cache
def redis_client() -> Redis | None:
    redis_url = get_settings().redis_url
    if not redis_url:
        return None
    return Redis.from_url(redis_url, decode_responses=True, health_check_interval=30)


async def enqueue_task(task_id: str) -> bool:
    client = redis_client()
    if client is None:
        return False
    try:
        await client.lpush(get_settings().task_queue_name, task_id)
        return True
    except RedisError:
        return False


async def dequeue_task(timeout_seconds: float) -> str | None:
    client = redis_client()
    if client is None:
        return None
    try:
        item = await client.brpop(
            get_settings().task_queue_name,
            timeout=max(1, round(timeout_seconds)),
        )
        return item[1] if item else None
    except RedisError:
        return None


async def publish_user_event(user_id: str, payload: dict[str, Any]) -> bool:
    client = redis_client()
    if client is None:
        return False
    try:
        await client.publish(
            f"{get_settings().task_event_channel}:{user_id}",
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        return True
    except RedisError:
        return False


async def close_redis() -> None:
    client = redis_client()
    if client is not None:
        await client.aclose()
    redis_client.cache_clear()
