import asyncio
import json
import struct

import pytest

from app.services.video_concat import concatenate_videos, media_binary, run_media_command


@pytest.mark.skipif(not media_binary("ffmpeg") or not media_binary("ffprobe"), reason="FFmpeg required")
def test_concat_mixed_framerates_and_audio_preserves_order(tmp_path):
    async def run():
        first, second, output = (tmp_path / name for name in ("red.mp4", "blue.mp4", "joined.mp4"))
        await run_media_command(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(first),
        )
        await run_media_command(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=90x160:r=25:d=1",
            "-c:v",
            "libx264",
            str(second),
        )
        updates = []

        async def progress(percent, message):
            updates.append(percent)

        await concatenate_videos([first, second], output, resolution="720p", ratio="16:9", progress=progress)
        info = json.loads(
            await run_media_command(
                "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)
            )
        )
        assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
        assert 1.9 < float(info["format"]["duration"]) < 2.3
        for time, channel in (("0.4", 0), ("1.4", 2)):
            pixel = await run_media_command(
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                time,
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                "crop=2:2:640:360",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            )
            assert pixel[channel] > 180
        assert updates == [47, 85]
        levels = []
        for time in ("0.3", "1.4"):
            audio = await run_media_command(
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                time,
                "-i",
                str(output),
                "-t",
                "0.2",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "s16le",
                "pipe:1",
            )
            samples = struct.unpack(f"<{len(audio) // 2}h", audio)
            levels.append(sum(abs(sample) for sample in samples) / len(samples))
        assert levels[0] > 100  # The original first clip's tone survives.
        assert levels[1] < 5  # No audio from the previous clip leaks into the silent one.
        assert not list(tmp_path.glob("concat-*"))

    asyncio.run(run())


@pytest.mark.skipif(not media_binary("ffmpeg") or not media_binary("ffprobe"), reason="FFmpeg required")
def test_audio_padding_cannot_extend_video_timeline(tmp_path):
    async def run():
        first, second, output = (tmp_path / name for name in ("long-audio.mp4", "silent.mp4", "result.mp4"))
        await run_media_command(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:r=24:d=4.25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=9",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(first),
        )
        await run_media_command(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=160x90:r=25:d=1",
            "-c:v",
            "libx264",
            str(second),
        )

        async def progress(*args):
            pass

        await concatenate_videos([first, second], output, resolution="720p", ratio="16:9", progress=progress)
        info = json.loads(
            await run_media_command(
                "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)
            )
        )
        assert 5.2 < float(info["format"]["duration"]) < 5.4
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert int(video["nb_frames"]) == 158
        packets = json.loads(
            await run_media_command(
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_packets",
                "-show_entries",
                "packet=pts_time",
                "-of",
                "json",
                str(output),
            )
        )
        times = sorted(float(p["pts_time"]) for p in packets["packets"])
        assert max(b - a for a, b in zip(times, times[1:], strict=False)) < 0.035

    asyncio.run(run())
