"""SystemOne requests, including a TLS-verified fallback for broken Zen DNS."""

import asyncio
import ipaddress
import time

import httpx

_zen_address: tuple[str, float] | None = None
_request_lock: asyncio.Lock | None = None
_request_lock_loop = None
_last_request_at = 0.0
_MIN_INTERVAL_SECONDS = 0.35
_RATE_LIMIT_RETRIES = 3
_CONNECT_RETRIES = 2


def _lock():
    global _request_lock, _request_lock_loop
    loop = asyncio.get_running_loop()
    if _request_lock is None or _request_lock_loop is not loop:
        _request_lock = asyncio.Lock()
        _request_lock_loop = loop
    return _request_lock


async def post(config, payload, *, timeout=None):
    seconds = timeout if timeout is not None else config.timeout_seconds
    global _last_request_at
    async with _lock():
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request_at = time.monotonic()
            try:
                for connection_attempt in range(_CONNECT_RETRIES + 1):
                    try:
                        async with asyncio.timeout(seconds):
                            return await _post(config, payload, seconds)
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        # The inference has not been sent: retry the handshake,
                        # including transient TLS failures. Never duplicate a
                        # sent inference in this transport retry loop.
                        if connection_attempt >= _CONNECT_RETRIES:
                            raise
                        await asyncio.sleep(0.35 * (2 ** connection_attempt))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 or attempt >= _RATE_LIMIT_RETRIES:
                    raise
                retry_after = exc.response.headers.get("retry-after", "")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = 0.75 * (2 ** attempt)
                await asyncio.sleep(max(0.35, min(delay, 12.0)))
            except TimeoutError as exc:
                raise httpx.ReadTimeout("JEV request timed out") from exc


async def _post(config, payload, seconds):
    global _zen_address
    headers = {"Authorization": "Bearer " + config.api_key}
    is_zen = config.provider == "opencode_zen"

    async with httpx.AsyncClient(timeout=seconds) as client:

        async def send(address=None):
            url = httpx.URL(config.endpoint)
            if address:
                # Preserve the original Host AND TLS server name/certificate checks.
                response = await client.post(
                    url.copy_with(host=address),
                    headers={**headers, "Host": url.host},
                    extensions={"sni_hostname": url.host},
                    json=payload,
                )
            else:
                response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

        cached = _zen_address if is_zen and _zen_address and _zen_address[1] > time.monotonic() else None
        try:
            return await send(cached[0] if cached else None)
        except httpx.ConnectError as exc:
            # Only retry pre-request connection failures. Never retry an accepted,
            # timed-out inference, HTTP errors or redirects (which can cost twice).
            if not is_zen:
                raise
            if not cached and not any(
                marker in str(exc).lower()
                for marker in (
                    "hostname mismatch",
                    "name or service not known",
                    "getaddrinfo failed",
                    "nodename nor servname",
                    "temporary failure in name resolution",
                )
            ):
                raise
            _zen_address = None
            # This request contains no API credentials or user conversation data.
            dns = await client.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": "opencode.ai", "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
            dns.raise_for_status()
            for record in dns.json().get("Answer", []):
                if record.get("type") != 1 or record.get("name", "").rstrip(".") != "opencode.ai":
                    continue
                try:
                    address = ipaddress.ip_address(record.get("data", ""))
                except ValueError:
                    continue
                if not address.is_global or address.version != 4:
                    continue
                result = await send(str(address))
                ttl = max(1, min(int(record.get("TTL", 60)), 300))
                _zen_address = (str(address), time.monotonic() + ttl)
                return result
            raise httpx.ConnectError("Zen DNS returned no public address") from exc
