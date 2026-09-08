from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from app.services.composition_renderer import output_size


def media_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    local = Path(sys.prefix) / "Scripts" / f"{name}.exe"
    return str(local) if local.is_file() else None


async def run_media_command(*command: str) -> bytes:
    executable = media_binary(command[0]) or command[0]
    process = await asyncio.create_subprocess_exec(
        executable,
        *command[1:],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=7200)
        if process.returncode:
            raise RuntimeError("视频拼接失败：" + stderr.decode(errors="replace")[-1200:])
        return stdout
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def concatenate_videos(paths: list[Path], output: Path, *, resolution: str, ratio: str, progress):
    if not media_binary("ffmpeg") or not media_binary("ffprobe"):
        raise RuntimeError("视频拼接节点缺少 FFmpeg/FFprobe，请安装后重试")
    if not paths:
        raise RuntimeError("没有可拼接的视频")
    width, height = output_size(resolution, ratio)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="concat-", dir=output.parent) as directory:
        root = Path(directory)
        for index, source in enumerate(paths):
            info = json.loads(
                await run_media_command(
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(source),
                )
            )
            if not any(s["codec_type"] == "video" for s in info["streams"]):
                raise RuntimeError(f"第 {index + 1} 段文件没有视频画面")
            audio = any(s["codec_type"] == "audio" for s in info["streams"])
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
            if not audio:
                command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            command += [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0" if audio else "1:a:0",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,setpts=PTS-STARTPTS",
                "-af",
                "aresample=48000,asetpts=PTS-STARTPTS,apad",
                "-shortest",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ac",
                "2",
                "-ar",
                "48000",
                str(root / f"{index:06d}.mp4"),
            ]
            await run_media_command(*command)
            await progress(10 + int(75 * (index + 1) / len(paths)), f"已处理 {index + 1}/{len(paths)} 段视频")
        manifest = root / "list.txt"
        manifest.write_text(
            "".join(f"file '{index:06d}.mp4'\n" for index in range(len(paths))), encoding="utf-8"
        )
        combined = root / "combined.mp4"
        await run_media_command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(manifest),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(combined),
        )
        combined.replace(output)


async def execute_video_concat_task(task_id: str):
    from app.core.config import get_settings
    from app.db.models import ProjectFile, ProjectFileKind, TaskStatus
    from app.db.session import SessionLocal
    from app.services.object_storage import materialize_media_file, persist_media_file
    from app.services.task_events import publish_task_event, record_task_event
    from app.services.task_worker import (
        cleanup_media,
        owned_task_for_update,
        owns_running_task,
        record_progress,
    )

    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            return
        payload = dict(task.request_payload)
        output = (
            get_settings().uploads_root.resolve()
            / task.tenant_id
            / "projects"
            / task.project_id
            / "renders"
            / f"concat-{task.id}-{uuid4().hex}.mp4"
        )
    paths = [await materialize_media_file(item["storage_key"]) for item in payload["clips"]]
    await concatenate_videos(
        paths,
        output,
        resolution=payload["resolution"],
        ratio=payload["ratio"],
        progress=lambda percent, message: record_progress(task_id, percent, message),
    )
    key, url = await persist_media_file(output, "video/mp4")
    async with SessionLocal() as session:
        task = await owned_task_for_update(session, task_id)
        if not owns_running_task(task):
            await cleanup_media(key, output)
            return
        session.add(
            ProjectFile(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=task.project_id,
                name=payload["filename"],
                kind=ProjectFileKind.VIDEO,
                mime_type="video/mp4",
                storage_path=key,
                size_bytes=output.stat().st_size,
                editable=False,
                file_metadata={
                    "chapter_id": payload["chapter_id"],
                    "source_task_id": task.id,
                    "role": "video_concat",
                },
            )
        )
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {"storage_key": key, "media_url": url, "filename": payload["filename"]}
        event = record_task_event(
            session, task, status=TaskStatus.SUCCEEDED, progress=100, message="拼接视频已完成，可下载"
        )
        await session.commit()
        await publish_task_event(task, event)
