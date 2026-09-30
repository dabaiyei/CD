"""Local canvas media derivatives; source media is never overwritten."""
from __future__ import annotations

import asyncio
import json
import math
import tempfile
from pathlib import Path

from app.services.video_concat import media_binary, run_media_command
from app.services.video_replica import MAX_BYTES

LOCAL_INPUT = ("-protocol_whitelist", "file,pipe", "-format_whitelist",
               "mov,matroska,avi,mpegts,mpeg,ogg,flv,wav,mp3,flac,aac")


async def probe(path):
    return json.loads(await run_media_command("ffprobe", "-v", "error", *LOCAL_INPUT,
        "-show_streams", "-show_format", "-of", "json", str(path)))


def duration(info, kind):
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == kind), None)
    if not stream:
        raise ValueError(f"素材没有可读取的{'音轨' if kind == 'audio' else '视频画面'}")
    value = float(stream.get("duration") or info.get("format", {}).get("duration") or 0)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("素材时长无效")
    return value, stream


async def video_duration(path, info):
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream:
        raise ValueError("素材没有可读取的视频画面")
    try:
        value = float(stream.get("duration") or 0)
    except (ValueError, TypeError):
        value = 0
    if not math.isfinite(value) or value <= 0:
        # Container duration may include an audio track longer than the picture.
        packets = json.loads(await run_media_command("ffprobe", "-v", "error", *LOCAL_INPUT,
            "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(path)))["packets"]
        spans = [(float(p["pts_time"]), float(p["pts_time"]) + float(p.get("duration_time") or 0)) for p in packets if "pts_time" in p]
        value = max(end for _, end in spans) - min(start for start, _ in spans) if spans else 0
    if not math.isfinite(value) or value <= 0:
        raise ValueError("视频画面时长无效")
    return value


async def process(video: bytes, *, operation: str, audio: bytes | None = None,
                  start=0.0, end=None, audio_start=0.0, offset=0.0):
    if any(len(data) > MAX_BYTES for data in (video, audio) if data is not None):
        raise ValueError("单个素材不能超过100MB")
    if not media_binary("ffmpeg") or not media_binary("ffprobe"):
        raise ValueError("媒体处理需要本地 FFmpeg/FFprobe")
    with tempfile.TemporaryDirectory(prefix="canvas-media-") as directory:
        root = Path(directory)
        source = root / "source.video"
        await asyncio.to_thread(source.write_bytes, video)
        info = await probe(source)
        length = await video_duration(source, info)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", *LOCAL_INPUT, "-i", str(source)]
        if operation in {"extract_audio", "trim_video"}:
            finish = length if end is None else end
            if not 0 <= start < finish <= length + 0.001:
                raise ValueError("截取范围需位于视频内")
            command += ["-ss", str(start), "-t", str(min(finish, length) - start)]
            if operation == "extract_audio":
                duration(info, "audio")
                output, mime = root / "audio.m4a", "audio/mp4"
                command += ["-map", "0:a:0", "-vn", "-c:a", "aac"]
            else:
                output, mime = root / "clip.mp4", "video/mp4"
                command += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                            "-preset", "fast", "-crf", "18", "-fps_mode", "vfr", "-c:a", "aac"]
        elif operation == "mux_audio":
            if audio is None:
                raise ValueError("请选择要封装的音轨")
            target = root / "source.audio"
            await asyncio.to_thread(target.write_bytes, audio)
            audio_length, _ = duration(await probe(target), "audio")
            if not 0 <= audio_start < audio_length or not 0 <= offset < length:
                raise ValueError("音轨起点或视频对齐位置超出素材范围")
            output, mime = root / "mux.mp4", "video/mp4"
            video_stream = next(s for s in info["streams"] if s.get("codec_type") == "video")
            video_codec = ["-c:v", "copy"] if video_stream["codec_name"] in {"h264", "hevc", "av1", "vp9", "mpeg4"} else ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-fps_mode", "vfr"]
            command += [*LOCAL_INPUT, "-i", str(target), "-map", "0:v:0", "-map", "1:a:0",
                        *video_codec, "-c:a", "aac", "-af",
                        f"atrim=start={audio_start},asetpts=N/SR/TB,adelay={offset * 1000}:all=1,apad,asetpts=N/SR/TB",
                        "-t", str(length)]
        else:
            raise ValueError("不支持的媒体操作")
        command += ["-movflags", "+faststart", str(output)]
        await run_media_command(*command)
        result = await probe(output)
        result_length, result_stream = duration(result, "audio" if mime.startswith("audio/") else "video")
        return {"data": await asyncio.to_thread(output.read_bytes), "mime_type": mime,
                "duration": result_length, "width": result_stream.get("width"), "height": result_stream.get("height"),
                "source_start": start if operation != "mux_audio" else 0,
                "source_end": min(end if end is not None else length, length) if operation != "mux_audio" else length}
