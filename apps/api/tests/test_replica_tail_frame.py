import asyncio

from PIL import Image

from app.services.replica_direction import extract_tail_frame
from app.services.video_concat import run_media_command


def test_tail_frame_survives_video_audio_duration_gap(tmp_path):
    source, target = tmp_path / "tail.mp4", tmp_path / "tail.png"

    async def run():
        await run_media_command(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=64x64:r=2:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        )
        await extract_tail_frame(source, target)

    asyncio.run(run())
    with Image.open(target) as picture:
        r, g, b = picture.convert("RGB").getpixel((0, 0))
        assert r > 200 and g < 20 and b < 20
