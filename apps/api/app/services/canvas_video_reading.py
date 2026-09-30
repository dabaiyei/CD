"""Local video evidence for canvas text models, reusing Hypit's contact sheets."""

from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
from fractions import Fraction
from pathlib import Path

from app.services.replica_reading import contact_sheets
from app.services.video_concat import media_binary, run_media_command
from app.services.video_replica import MAX_BYTES, MAX_SECONDS

LOCAL_INPUT = (
    "-protocol_whitelist",
    "file,pipe",
    "-format_whitelist",
    "mov,matroska,avi,mpegts,mpeg,ogg,flv",
)


async def extract(data: bytes, *, count=16, start=0.0, end=None, sampling="adaptive", times=None):
    if not isinstance(count, int) or not 2 <= count <= 32:
        raise ValueError("取帧数量必须为2到32")
    if len(data) > MAX_BYTES:
        raise ValueError("单个视频不能超过100MB")
    if not media_binary("ffmpeg") or not media_binary("ffprobe"):
        raise ValueError("视频分析需要本地 FFmpeg/FFprobe，请检查服务部署")
    with tempfile.TemporaryDirectory(prefix="canvas-video-") as directory:
        root = Path(directory)
        source = root / "source.video"
        await asyncio.to_thread(source.write_bytes, data)
        probe = json.loads(
            await run_media_command(
                "ffprobe",
                "-v",
                "error",
                *LOCAL_INPUT,
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(source),
            )
        )
        streams = probe.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video:
            raise ValueError("素材没有可读取的视频画面")
        from app.services.canvas_media import video_duration

        duration = await video_duration(source, probe)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("无法读取视频时长")
        finish = duration if end is None else end
        if not 0 <= start < finish <= duration + 0.001 or finish - start > MAX_SECONDS:
            raise ValueError(f"分析范围需位于视频内，单次不超过{MAX_SECONDS}秒；长视频可按时间范围分段分析")
        frames = []
        try:
            rate = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"))
        except (ValueError, ZeroDivisionError):
            rate = 0
        # A timestamp after the final decoded frame yields an empty image, even before container duration.
        last_frame = max(0, duration - (1 / rate if rate > 0 else 0.1) - 0.0001)
        if last_frame < start:
            raise ValueError("分析范围没有可解码画面")
        transitions = []
        transition_count = 0
        moments = [min(start + (min(finish, duration) - start) * (i + 0.5) / count, last_frame) for i in range(count)]
        roles = {}
        if times is not None:
            if not times or len(times) > 32 or any(not math.isfinite(t) or not start <= t < finish or t > last_frame + 0.001 for t in times):
                raise ValueError("补取时间点必须位于分析范围和可解码帧内，最多32个")
            moments = sorted(set(min(t, last_frame) for t in times))
            sampling = "exact"
        elif sampling == "adaptive":
            metadata = (await run_media_command("ffmpeg", "-hide_banner", "-loglevel", "error",
                *LOCAL_INPUT, "-ss", str(start), "-i", str(source), "-t", str(finish - start),
                "-vf", "scale=160:90,scdet,metadata=mode=print:file=-", "-an", "-f", "null", "-")).decode()
            detected = []
            previous = start
            for block in metadata.split("frame:")[1:]:
                stamp = re.search(r"pts_time:([\d.e+-]+)", block)
                score = re.search(r"lavfi.scd.score=([\d.]+)", block)
                if stamp and score and "lavfi.scd.time=" in block:
                    at = start + float(stamp[1])
                    if start < at <= last_frame and at < finish:
                        detected.append({"at": at, "before": previous, "score": float(score[1])})
                if stamp:
                    previous = start + float(stamp[1])
            if detected:
                chosen = set()
                for change in sorted(detected, key=lambda c: -c["score"]):
                    before, after = change["before"], change["at"]
                    if len(chosen | {before, after}) <= count:
                        chosen.update((before, after))
                        roles[before], roles[after] = "before_transition", "after_transition"
                        transitions.append({**change, "before": before, "after": after})
                for at in (start, min(finish, last_frame)):
                    if len(chosen) < count:
                        chosen.add(at)
                for at in sorted(moments, key=lambda t: -min(abs(t - x) for x in chosen)):
                    if len(chosen) >= count:
                        break
                    chosen.add(at)
                moments = sorted(chosen)
            transition_count = len(detected)
        elif sampling != "uniform":
            raise ValueError("不支持的取帧策略")
        # FFmpeg's accurate input seek decodes from the preceding keyframe to each requested time.
        for index, at in enumerate(moments):
            path = root / f"{index:03d}.webp"
            await run_media_command(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                *LOCAL_INPUT,
                "-ss",
                f"{max(0, at - 0.000001):.6f}",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=1024:1024:force_original_aspect_ratio=decrease",
                "-c:v",
                "libwebp",
                "-quality",
                "85",
                str(path),
            )
            if not path.is_file() or not path.stat().st_size:
                raise ValueError(f"无法提取 {at:.3f} 秒画面，未发送空证据")
            frames.append({"at": at, "path": str(path), "role": roles.get(at, "overview")})
        sheets = await asyncio.to_thread(contact_sheets, frames)
        raw = [{"at": f["at"], "role": f["role"], "data": await asyncio.to_thread(Path(f["path"]).read_bytes)} for f in frames]
        return {
            "duration": duration,
            "start": start,
            "end": min(finish, duration),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "frames": raw,
            "sheets": sheets,
            "sampling": sampling,
            "transitions": sorted(transitions, key=lambda c: c["at"]),
            "transition_count": transition_count if sampling == "adaptive" else 0,
        }
