from pathlib import Path
from types import SimpleNamespace

import httpx

from app.api.routes import canvas_agent
from app.services.canvas_agent import AgentConnection, account_directory


def test_canvas_agent_start_and_proxy_are_account_scoped(client, admin_headers, creator_headers, monkeypatch):
    upstream_token = "private-local-agent-secret"
    launched = []

    async def start(tenant_id, user_id):
        launched.append((tenant_id, user_id))
        return AgentConnection("http://127.0.0.1:17371", upstream_token, Path("test-agent"))

    monkeypatch.setattr(canvas_agent, "start_agent", start)
    monkeypatch.setattr(
        canvas_agent, "mcp_config", lambda _: {"command": "node", "args": ["entry.js", "mcp"]}
    )
    assert client.post("/api/v1/canvas/agent/start", headers=creator_headers).status_code == 403
    assert client.post("/api/v1/canvas/agent/start").status_code == 401
    result = client.post("/api/v1/canvas/agent/start", headers=admin_headers)
    assert result.status_code == 200, result.text
    assert result.json()["url"] == "/api/v1/canvas/agent"
    assert upstream_token not in result.text
    token = result.json()["token"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert client.get("/api/v1/canvas/agent/health").status_code == 401
    monkeypatch.setattr(
        canvas_agent,
        "existing_connection",
        lambda tenant, user: AgentConnection("http://127.0.0.1:17371", upstream_token, Path("test-agent")),
    )
    original_client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.headers["x-canvas-agent-token"] == upstream_token
        assert "token" not in request.url.params
        if request.url.path == "/events":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=b'event: hello\ndata: {"protocolVersion":6}\n\n',
            )
        return httpx.Response(200, json={"ok": True, "workspace": {"activeThreadId": "canvas-thread"}})

    monkeypatch.setattr(
        canvas_agent.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    url = "/api/v1/canvas/agent/agent/codex/workspace?token=" + token
    response = client.get(url)
    assert response.status_code == 200 and response.json()["workspace"]["activeThreadId"] == "canvas-thread"
    events = client.get("/api/v1/canvas/agent/events?token=" + token + "&clientId=owner-client")
    assert events.status_code == 200 and "event: hello" in events.text
    assert events.headers["X-Accel-Buffering"] == "no"
    assert requests[-1].url.params["clientId"] == "owner-client"
    settings = client.get("/api/v1/canvas/agent/connection", headers=admin_headers)
    assert settings.status_code == 200 and settings.json()["mcp"]["command"] == "node"
    assert upstream_token not in settings.text
    assert client.get("/api/v1/canvas/agent/connection", headers=creator_headers).json() == {
        "available": False,
        "running": False,
    }
    assert len(launched) == 1


def test_canvas_agent_bridge_rejects_other_roles_and_wrong_token_type(client, creator_headers):
    user = client.get("/api/v1/auth/me", headers=creator_headers).json()["user"]
    token = canvas_agent.bridge_token(SimpleNamespace(id=user["id"], tenant_id=user["tenant_id"]))
    assert client.get("/api/v1/canvas/agent/health?token=" + token).status_code == 403
    access = creator_headers["Authorization"].split(" ", 1)[1]
    assert client.get("/api/v1/canvas/agent/health?token=" + access).status_code == 401
    assert account_directory("tenant", "user-a") != account_directory("tenant", "user-b")
    assert account_directory("tenant-a", "user") != account_directory("tenant-b", "user")
