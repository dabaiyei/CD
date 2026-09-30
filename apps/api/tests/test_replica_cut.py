import asyncio

import pytest

from app.services import hypit_bridge, replica_cut
from app.services.video_concat import run_media_command


def test_cut_native_compile_preserves_assets_and_applies_timing(tmp_path):
    source = tmp_path / "clip.mp4"

    async def run():
        await run_media_command(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=160x90:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:a",
            "aac",
            "-c:v",
            "libx264",
            str(source),
        )
        root = tmp_path / "production"
        hypit_bridge.write_composition(root, [{"path": str(source), "duration": 3}], width=160, height=90)
        value = {
            "revision": "x" * 64,
            "files": {
                n: (root / n).read_text(encoding="utf-8")
                for n in ("composition.svml", "look.svs", "render.svrun")
            },
            "assets": {"assets/clip-0.mp4": {"type": "video", "key": "owned.mp4", "duration": 3}},
        }
        clips = [
            replica_cut.Cut(asset="assets/clip-0.mp4", start=1, end=2, volume=0),
            replica_cut.Cut(asset="assets/clip-0.mp4", start=0, end=0.5, volume=0.5),
        ]
        edited = replica_cut.apply(value, clips)
        assert edited["assets"] == value["assets"]
        assert replica_cut.read(edited) == [c.model_dump() for c in clips]
        assert 'end="45f"' in edited["files"]["composition.svml"]
        for name, content in edited["files"].items():
            (root / name).write_text(content, encoding="utf-8")
        await hypit_bridge.command("check", "render.svrun", "--json", workspace=root)
        for endpoint in ["media.local", "hyperframes.local"]:
            await hypit_bridge.command(
                "programs",
                "prepare",
                "--runtime",
                "hypit.runtime.json",
                "--endpoint",
                endpoint,
                "--json",
                workspace=root,
            )
        try:
            report = await hypit_bridge.command(
                "build",
                "render.svrun",
                "--runtime",
                "hypit.runtime.json",
                "--follow",
                "--json",
                workspace=root,
                timeout=180,
            )
            assert report["build"]["result"]["state"] == "complete", report
            final = root / "cut.mp4"
            await hypit_bridge.command(
                "get",
                report["build"]["id"],
                "--output",
                "final.video",
                "--to",
                str(final),
                "--json",
                workspace=root,
            )
            import json

            info = json.loads(
                await run_media_command("ffprobe", "-v", "error", "-show_format", "-of", "json", str(final))
            )
            assert float(info["format"]["duration"]) == pytest.approx(1.5, abs=0.08)
        finally:
            await hypit_bridge.command(
                "runtime", "down", "--runtime", "hypit.runtime.json", "--json", workspace=root
            )
        with pytest.raises(ValueError):
            replica_cut.apply(value, [replica_cut.Cut(asset="assets/clip-0.mp4", start=0, end=9)])
        advanced = {
            **value,
            "files": {
                **value["files"],
                "composition.svml": value["files"]["composition.svml"].replace(
                    "<svml>", '<svml><import from="@hypit/caption-fine@1"/>'
                ),
            },
        }
        assert replica_cut.view(advanced)["reason"]
        gap = {
            **edited,
            "files": {
                **edited["files"],
                "composition.svml": edited["files"]["composition.svml"].replace('at="30f"', 'at="32f"'),
            },
        }
        assert replica_cut.view(gap)["timing_warning"]
        with pytest.raises(ValueError, match="紧密拼接"):
            replica_cut.apply(gap, clips)
        assert not replica_cut.view(replica_cut.apply(gap, clips, close_gaps=True))["timing_warning"]

    asyncio.run(run())
