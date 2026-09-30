"""Free live smoke: bundled Agent, Codex model list, SSE and MCP canvas tools.

Run with the local API running. Uses an admin account without printing tokens;
never sends a model turn or modifies existing canvas documents.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
from app.core.security import create_access_token
from app.db.models import User, UserRole
from app.db.session import SessionLocal


async def main():
    async with SessionLocal() as db:
        user = await db.scalar(
            select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        if not user:
            raise RuntimeError("No active admin account")
        token = create_access_token(
            user_id=user.id, tenant_id=user.tenant_id, role=user.role.value
        )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000/api/v1", timeout=60, trust_env=False
    ) as client:
        started = await client.post(
            "/canvas/agent/start", headers={"Authorization": f"Bearer {token}"}
        )
        started.raise_for_status()
        connection = started.json()
        bridge = "http://127.0.0.1:8000" + connection["url"]
        params = {"token": connection["token"], "clientId": "local-integration-probe"}
        workspace = await client.get(bridge + "/agent/codex/workspace", params=params)
        workspace.raise_for_status()
        assert workspace.json()["ok"]
        models = await client.get(bridge + "/agent/codex/models", params=params)
        models.raise_for_status()
        assert models.json()["ok"] and models.json()["data"]
        print(
            f"Bundled Agent and real Codex model list: {len(models.json()['data'])} models"
        )
        hello = asyncio.Event()
        updated = asyncio.Event()
        snapshot = {
            "projectId": "isolated-probe",
            "nodes": [],
            "connections": [],
            "selectedNodeIds": [],
            "viewport": {"x": 0, "y": 0, "k": 1},
        }

        async def receive_events():
            async with client.stream(
                "GET", bridge + "/events", params=params
            ) as stream:
                stream.raise_for_status()
                event = ""
                async for line in stream.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        payload = json.loads(line[6:])
                        if event == "hello":
                            assert payload["protocolVersion"] == 6
                            hello.set()
                        elif event == "tool_call":
                            assert payload["name"] == "canvas_apply_ops"
                            for operation in payload["input"]["ops"]:
                                assert operation["type"] == "add_node"
                                snapshot["nodes"].append(
                                    {
                                        "id": "probe-node",
                                        "type": operation["nodeType"],
                                        "position": operation["position"],
                                        "width": 340,
                                        "height": 240,
                                        "metadata": operation["metadata"],
                                    }
                                )
                            response = await client.post(
                                bridge + "/canvas/result",
                                params=params,
                                json={
                                    "requestId": payload["requestId"],
                                    "result": snapshot,
                                },
                            )
                            response.raise_for_status()
                            (
                                await client.post(
                                    bridge + "/canvas/state",
                                    params=params,
                                    json=snapshot,
                                )
                            ).raise_for_status()
                            updated.set()

        receiving = asyncio.create_task(receive_events())
        process = None
        try:
            await asyncio.wait_for(hello.wait(), 10)
            (
                await client.post(
                    bridge + "/canvas/state", params=params, json=snapshot
                )
            ).raise_for_status()
            mcp = connection["mcp"]
            process = await asyncio.create_subprocess_exec(
                mcp["command"],
                *mcp["args"],
                env=dict(os.environ, **mcp["env"]),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async def rpc(request):
                process.stdin.write((json.dumps(request) + "\n").encode())
                await process.stdin.drain()
                if "id" not in request:
                    return
                while True:
                    line = await asyncio.wait_for(process.stdout.readline(), 15)
                    if not line:
                        raise RuntimeError("MCP process exited")
                    result = json.loads(line)
                    if result.get("id") == request["id"]:
                        assert "error" not in result, result
                        return result["result"]

            await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "cineforge-integration-probe",
                            "version": "1",
                        },
                    },
                }
            )
            await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listing = await rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert any(
                tool["name"] == "canvas_generate_video" for tool in listing["tools"]
            )
            written = await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "canvas_create_text_node",
                        "arguments": {
                            "text": "MCP bridge smoke test",
                            "title": "Isolated test",
                        },
                    },
                }
            )
            assert not written.get("isError"), written
            assert len(snapshot["nodes"]) == 1
            await asyncio.wait_for(updated.wait(), 10)
            read = await rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "canvas_get_state", "arguments": {}},
                }
            )
            assert (
                json.loads(read["content"][0]["text"])["nodes"][0]["metadata"][
                    "content"
                ]
                == "MCP bridge smoke test"
            )
            print(
                f"Live same-origin SSE and {len(listing['tools'])} MCP tools: read/write/result roundtrip passed; no inference sent"
            )
        finally:
            receiving.cancel()
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
            with contextlib.suppress(asyncio.CancelledError):
                await receiving


if __name__ == "__main__":
    asyncio.run(main())
