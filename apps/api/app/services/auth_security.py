from __future__ import annotations

import hashlib
import hmac

from redis.exceptions import RedisError

from app.core.config import get_settings
from app.services.task_queue import redis_client


def private_fingerprint(value: str) -> str:
    settings = get_settings()
    return hmac.new(settings.jwt_secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def login_identity_hash(tenant: str, email: str) -> str:
    return private_fingerprint(f"{tenant.strip().lower()}:{email.strip().lower()}")


async def consume_login_rate_limit(client_ip: str) -> int | None:
    """Return retry seconds when an IP has exhausted the shared Redis login budget."""
    client = redis_client()
    if client is None:
        return None
    settings = get_settings()
    key = f"cineforge:auth:login-rate:{private_fingerprint(client_ip)}"
    script = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return {count, redis.call('TTL', KEYS[1])}
    """
    try:
        count, ttl = await client.eval(script, 1, key, settings.login_rate_window_seconds)
    except RedisError:
        return None
    if int(count) <= settings.login_rate_limit:
        return None
    return max(1, int(ttl))
