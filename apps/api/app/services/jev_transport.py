"""SystemOne requests, including a TLS-verified fallback for broken Zen DNS."""

import asyncio
import ipaddress
import time

import httpx

_zen_address: tuple[str, float] | None = None


async def post(config, payload, *, timeout=None):
    seconds = timeout if timeout is not None else config.timeout_seconds
    try:
        async with asyncio.timeout(seconds):
            return await _post(config, payload, seconds)
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
