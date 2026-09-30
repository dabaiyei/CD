"""Run the bundled local Canvas Agent and isolate its workspace by account."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings


@dataclass
class AgentConnection:
    url: str
    token: str
    directory: Path


_locks: dict[str, asyncio.Lock] = {}


def account_directory(tenant_id: str, user_id: str) -> Path:
    key = hashlib.sha256(f"{tenant_id}:{user_id}".encode()).hexdigest()
    return get_settings().site_backup_runtime_root / "canvas-agent" / key


def existing_connection(tenant_id: str, user_id: str) -> AgentConnection | None:
    directory = account_directory(tenant_id, user_id)
    file = directory / "canvas-agent.json"
    if not file.exists():
        return None
    config = json.loads(file.read_text(encoding="utf-8"))
    url = urlsplit(config["url"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or not config.get("token"):
        raise ValueError("Canvas Agent 配置损坏，请恢复该账号的配置备份")
    return AgentConnection(config["url"], config["token"], directory)


async def is_alive(connection: AgentConnection) -> bool:
    try:
        async with httpx.AsyncClient(timeout=6, trust_env=False) as client:
            response = await client.get(
                f"{connection.url}/agent/codex/workspace",
                headers={"x-canvas-agent-token": connection.token},
            )
            return response.status_code == 200 and response.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        return False


async def start_agent(tenant_id: str, user_id: str) -> AgentConnection:
    directory = account_directory(tenant_id, user_id)
    lock = _locks.setdefault(str(directory), asyncio.Lock())
    async with lock:
        existing = existing_connection(tenant_id, user_id)
        if existing and await is_alive(existing):
            return existing
        settings = get_settings()
        entry = settings.canvas_agent_entry.resolve()
        node = shutil.which(settings.canvas_agent_node)
        if not node or not entry.is_file():
            raise RuntimeError(
                "本机 Canvas Agent 尚未构建，请运行 pnpm install 和 pnpm build:canvas-agent，"
                "或连接你电脑上的 Agent"
            )
        directory.mkdir(parents=True, exist_ok=True)
        file = directory / "canvas-agent.json"
        config = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config.update(url=f"http://127.0.0.1:{port}", token=config.get("token") or secrets.token_hex(24))
        config.setdefault("workspace", {"workspacePath": str(directory / "workspace")})
        # Preserve active threads and metadata when reusing a stopped service.
        temp = file.with_suffix(".tmp")
        temp.write_text(json.dumps(config), encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(file)
        env = dict(os.environ, INFINITE_CANVAS_AGENT_HOME=str(directory), PORT=str(port))
        codex = settings.canvas_agent_codex_bin or shutil.which("codex.exe") or shutil.which("codex")
        if codex and not codex.endswith((".cmd", ".ps1")):
            env["INFINITE_CANVAS_CODEX_BIN"] = codex
        with (directory / "service.log").open("ab") as log:
            process = subprocess.Popen(
                [node, str(entry)],
                cwd=entry.parent,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        connection = AgentConnection(config["url"], config["token"], directory)
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("Canvas Agent 启动失败，请检查该账号目录中的 service.log")
            if await is_alive(connection):
                return connection
            await asyncio.sleep(0.25)
        process.terminate()
        raise RuntimeError("Canvas Agent 尚未就绪，请检查服务日志后重试")


def mcp_config(connection: AgentConnection) -> dict:
    settings = get_settings()
    return {
        "command": shutil.which(settings.canvas_agent_node) or settings.canvas_agent_node,
        "args": [str(settings.canvas_agent_entry.resolve()), "mcp"],
        "env": {"INFINITE_CANVAS_AGENT_HOME": str(connection.directory)},
    }
