import asyncio
import json

import httpx
import pytest

from app.services import jev_transport
from app.services.jev_configuration import JevSettings


@pytest.fixture(autouse=True)
def clear_dns_cache():
    jev_transport._zen_address = None
    yield
    jev_transport._zen_address = None


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )


def test_zen_dns_fallback_keeps_tls_identity_and_never_sends_key_to_dns(monkeypatch):
    calls = []
    payload = {"model": "jev-1.13-free", "state": "private message", "questions": {}}

    def handler(request):
        calls.append(request)
        if request.url.host == "opencode.ai":
            raise httpx.ConnectError("certificate verify failed: Hostname mismatch")
        if request.url.host == "cloudflare-dns.com":
            assert "authorization" not in request.headers
            assert b"private message" not in request.content and "secret" not in str(request.url)
            return httpx.Response(
                200, json={"Answer": [{"name": "opencode.ai.", "type": 1, "TTL": 60, "data": "172.65.90.21"}]}
            )
        assert request.url.host == "172.65.90.21"
        assert request.headers["host"] == "opencode.ai"
        assert request.extensions["sni_hostname"] == "opencode.ai"
        assert request.headers["authorization"] == "Bearer secret"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json={"answers": {}})

    mock_client(monkeypatch, handler)

    async def run():
        config = JevSettings(True, "secret", provider="opencode_zen")
        await jev_transport.post(config, payload)
        await jev_transport.post(config, payload)

    asyncio.run(run())
    assert len(calls) == 4  # DNS resolution reused within its bounded TTL.


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow inference"), 429, 503])
def test_never_retries_sent_inference(monkeypatch, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure, json={"error": "unavailable"})

    mock_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPError):
        asyncio.run(jev_transport.post(JevSettings(True, "secret", provider="opencode_zen"), {}))
    assert len(calls) == 1


def test_dns_rejects_nonpublic_answers(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "opencode.ai":
            raise httpx.ConnectError("getaddrinfo failed")
        assert request.url.host == "cloudflare-dns.com"
        return httpx.Response(200, json={"Answer": [{"name": "opencode.ai", "type": 1, "data": "127.0.0.1"}]})

    mock_client(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError, match="no public address"):
        asyncio.run(jev_transport.post(JevSettings(True, "secret", provider="opencode_zen"), {}))
    assert len(calls) == 2


def test_typesafe_does_not_use_zen_dns(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("Hostname mismatch")

    mock_client(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(jev_transport.post(JevSettings(True, "secret"), {}))
    assert len(calls) == 1
