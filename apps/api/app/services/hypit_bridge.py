"""Pinned local Hypit tools. Never execute shell text or model-authored code."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

from app.core.config import PROJECT_ROOT

INSTALL = PROJECT_ROOT / "services/video-replica"
CLI = INSTALL / "node_modules/@hypit/hypit/bin/hypit.mjs"


def available() -> bool:
    return CLI.is_file() and shutil.which("node") is not None


async def command(*args: str, workspace: Path, progress=None, timeout=600, env_overrides=None):
    if not available():
        raise RuntimeError("视频复刻依赖未安装，请在 services/video-replica 执行 npm ci")
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.setdefault("UV_PYTHON", sys.executable)
    env.update(env_overrides or {})
    process = await asyncio.create_subprocess_exec(
        shutil.which("node"),
        str(CLI),
        *args,
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **({"creationflags": 0x08000000} if os.name == "nt" else {}),
    )
    pending = asyncio.create_task(process.communicate())
    try:
        async with asyncio.timeout(timeout):
            while not pending.done():
                await asyncio.wait({pending}, timeout=15)
                if progress and not pending.done():
                    await progress()
            stdout, stderr = await pending
        if process.returncode and args and args[0] == "status":
            # Native status returns a nonzero exit for terminal failed builds.
            # Preserve its structured state so callers can reset only this build.
            try:
                report = json.loads(stdout)
                if report.get("format") == "hypit.cli-status@1" and isinstance(report.get("build"), dict):
                    return report
            except (ValueError, TypeError):
                pass
        if process.returncode:
            raise RuntimeError("Hypit 执行失败：" + (stderr or stdout).decode(errors="replace")[-1800:])
        return json.loads(stdout) if stdout.strip() else {}
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


def write_composition(root: Path, clips: list[dict], *, width: int, height: int):
    """Generate trusted SVML from typed media, not arbitrary model-generated markup."""
    (root / "assets").mkdir(parents=True, exist_ok=True)
    lines = ['<?svml using="@hypit/markup@1"?>', "<svml>"]
    for alias, package in [
        ("asset", "media"),
        ("pipeline", "media-pipeline"),
        ("time", "timeline-author"),
        ("space", "spatial"),
        ("track", "media-track"),
        ("film", "film"),
        ("render", "render-hyperframes"),
    ]:
        lines.append(f'<import as="{alias}" from="@hypit/{package}@1"/>')
    lines += [
        '<import as="look" source="./look.svs"/>',
        '<time:Clock id="clock" frame-rate="30"/>',
        f'<space:Canvas id="canvas" width="{width}" height="{height}"/>',
        '<space:Frame id="full" within={canvas} left="0%" top="0%" right="100%" bottom="100%"/>',
    ]
    total = sum(round(float(c["duration"]) * 30) for c in clips)
    for i, clip in enumerate(clips):
        target = root / "assets" / f"clip-{i}.mp4"
        if Path(clip["path"]).resolve() != target.resolve():
            shutil.copyfile(clip["path"], target)
        src = quoteattr(f"./assets/clip-{i}.mp4")
        lines += [
            f'<asset:Video id="clip-{i}" src={src}/>',
            f'<pipeline:Normalize id="media-{i}" source={{clip-{i}}} clock={{clock}} '
            'video="primary-moving" audio="default" span-authority="video"/>',
        ]
    # Declare prepared material before Timeline so a later semantic Take can
    # reuse it without forward references (Hypit sources resolve in source order).
    lines.append(f'<time:Timeline id="timeline" clock={{clock}} end="{total}f"/>')
    lines.append('<track:Track id="picture" timeline={timeline.timeline} canvas={canvas}>')
    start = 0
    for i, clip in enumerate(clips):
        frames = round(float(clip["duration"]) * 30)
        lines.append(
            f'<track:Item id="shot-{i}" media={{media-{i}.media}} frame={{full}} '
            f'at="{start}f" for="{frames}f" source-audio="content" appearance={{look.media.full}}/>'
        )
        start += frames
    lines += [
        "</track:Track>",
        '<film:Film id="main" canvas={canvas} timeline={timeline.timeline} appearance={look.film.main}>',
        "<film:Track source={picture.visual}/><film:Track source={picture.audio}/></film:Film>",
        '<render:Video id="final" composition={main.composition} timeline={timeline.timeline}/>',
        "</svml>",
    ]
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"private":true,"type":"module"}', encoding="utf-8")
    (root / "composition.svml").write_text("\n".join(lines), encoding="utf-8")
    (root / "look.svs").write_text(
        '<?svml using="@hypit/svs@1"?>\n<sheet version="1">\n'
        "media.full { stack-order: 0; fit: contain; }\nfilm.main { background: #000000; }\n</sheet>",
        encoding="utf-8",
    )
    (root / "render.svrun").write_text(
        '<?svml using="@hypit/run-markup@1"?>\n'
        '<svrun version="1"><author source="./composition.svml"/><target output="final.video"/></svrun>',
        encoding="utf-8",
    )
    (root / "hypit.runtime.json").write_text(
        json.dumps(
            {
                "format": "hypit.runtime-local@1",
                "dataRoot": ".hypit/runtime-data",
                "credentials": {},
                "bindings": {},
                "endpoints": {
                    "media.local": {"use": "@hypit/provider-media-local"},
                    "hyperframes.local": {
                        "use": "@hypit/provider-hyperframes-local",
                        "config": {"workers": 1, "defaultConcurrency": 1, "browserCapacity": 1},
                    },
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
