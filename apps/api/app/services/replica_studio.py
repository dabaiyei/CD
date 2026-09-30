"""Short-lived loopback Hypit Studio sessions, exposed only through a scoped gateway."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import httpx

from app.services import hypit_bridge, replica_production


@dataclass
class Studio:
    id: str
    ticket: str
    tenant_id: str
    user_id: str
    task_id: str
    production: dict
    temporary: tempfile.TemporaryDirectory
    root: Path
    port: int
    process: asyncio.subprocess.Process
    log: list[str] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def origin(self):
        return f"http://localhost:{self.port}"


sessions: dict[str, Studio] = {}
_guards: set[asyncio.Task] = set()


async def close(studio):
    sessions.pop(studio.id, None)
    if studio.process.returncode is None:
        studio.process.kill()
        await studio.process.wait()
    with suppress(RuntimeError, TimeoutError):
        await hypit_bridge.command(
            "runtime",
            "down",
            "--runtime",
            str(studio.root / "hypit.runtime.json"),
            "--json",
            workspace=studio.root,
            timeout=15,
        )
    # A renderer may still be releasing files; never remove a broader directory.
    with suppress(OSError):
        studio.temporary.cleanup()


async def shutdown():
    for guard in list(_guards):
        guard.cancel()
    await asyncio.gather(*list(_guards), return_exceptions=True)
    await asyncio.gather(*(close(studio) for studio in list(sessions.values())), return_exceptions=True)


async def start(task, user, value):
    for old in list(sessions.values()):
        if (old.user_id, old.tenant_id, old.task_id) == (user.id, user.tenant_id, task.id):
            old.touched = time.monotonic()
            return old
    if len(sessions) >= 8:
        raise ValueError("本机同时打开的制作编辑器已达8个，请关闭闲置编辑器")
    folder = tempfile.TemporaryDirectory(prefix="hypit-studio-")
    root = Path(folder.name)
    try:
        await replica_production.materialize(value, root)
        await replica_production.prepare_fonts(root)
        await replica_production.prepare_speech(root)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = dict(os.environ)
        env.pop("INIT_CWD", None)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        env.setdefault("UV_PYTHON", sys.executable)
        process = await asyncio.create_subprocess_exec(
            shutil.which("node"),
            str(hypit_bridge.CLI),
            "studio",
            "--run",
            str(root / "render.svrun"),
            "--runtime",
            str(root / "hypit.runtime.json"),
            "--workspace",
            str(root),
            "--port",
            str(port),
            cwd=root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}),
        )
        studio = Studio(
            secrets.token_hex(16),
            secrets.token_urlsafe(32),
            user.tenant_id,
            user.id,
            task.id,
            value,
            folder,
            root,
            port,
            process,
        )
        sessions[studio.id] = studio

        async def read_log():
            async for line in process.stdout:
                studio.log.append(line.decode(errors="replace").rstrip())
                studio.log[:] = studio.log[-25:]

        reader = asyncio.create_task(read_log())
        _guards.add(reader)
        reader.add_done_callback(_guards.discard)
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            async with asyncio.timeout(120):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError("Hypit Studio 启动失败：" + "\n".join(studio.log)[-1800:])
                    # Wait for this process's announced port, never proxy an unrelated localhost service.
                    announced = "\n".join(studio.log)
                    if f"localhost:{port}" in announced or f"127.0.0.1:{port}" in announced:
                        response = await client.get(studio.origin + "/")
                        if response.is_success:
                            break
                    await asyncio.sleep(0.3)

        async def reap():
            while studio.id in sessions:
                await asyncio.sleep(30)
                if time.monotonic() - studio.touched > 1800:
                    await close(studio)
                    break

        guard = asyncio.create_task(reap())
        _guards.add(guard)
        guard.add_done_callback(_guards.discard)
        return studio
    except BaseException:
        if "studio" in locals():
            await close(studio)
        else:
            folder.cleanup()
        raise


def permitted_path(path, studio):
    decoded = unquote(path).replace("\\", "/")
    if "\x00" in decoded or ".." in decoded.split("/") or decoded.startswith("/"):
        return False
    if decoded.startswith("@fs/"):
        absolute = Path(decoded[4:]).resolve()
        dependencies = hypit_bridge.INSTALL / "node_modules"
        return absolute.is_relative_to(dependencies.resolve()) or absolute.is_relative_to(
            studio.root.resolve()
        )
    # Vite's root is the installed Studio, not the user's repository.
    return not any(part.startswith(".") and part not in (".vite",) for part in decoded.split("/"))


def rewrite(content: str, prefix: str, *, html=False):
    """Rebase native Vite modules, resources and Studio's absolute endpoints."""
    content = re.sub(
        r"([\"\'`])/(?=__studio(?:/|[\"\'`?])|@(?:vite|fs|id)/|src/|locales/|node_modules/)",
        lambda m: m[1] + prefix + "/",
        content,
    )
    if html:
        shim = """<script>(()=>{const base=PREFIX;const Native=window.WebSocket;
window.WebSocket=class extends Native{constructor(url,protocols){const u=new URL(url,location.href);
u.protocol=location.protocol==='https:'?'wss:':'ws:';u.host=location.host;u.pathname=base+'/socket';
super(u.href,protocols)}};
const fetch_=window.fetch;window.fetch=(input,opts)=>{
if(typeof input==='string'&&input.startsWith('/')&&!input.startsWith(base+'/'))input=base+input;
return fetch_(input,opts)};
})();</script>""".replace("PREFIX", json.dumps(prefix))
        content = content.replace("<head>", "<head>" + shim, 1)
    return content


def edited_files(studio):
    files = dict(studio.production["files"])
    for name in replica_production.SOURCE_FILES:
        files[name] = (studio.root / name).read_text(encoding="utf-8")
    feedback = studio.root / "FEEDBACK.json"
    if feedback.exists():
        files["FEEDBACK.json"] = feedback.read_text(encoding="utf-8")
    replica_production.validate_sources(files, studio.production["assets"])
    return files
