"""Exercise the running canvas extraction API with disposable, owned media; no inference."""

import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
from app.core.security import create_access_token
from app.db.models import User, UserRole
from app.db.session import SessionLocal
from app.services.video_concat import run_media_command
from sqlalchemy import select


async def main():
    async with SessionLocal() as db:
        user = await db.scalar(
            select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        token = create_access_token(
            user_id=user.id, tenant_id=user.tenant_id, role=user.role.value
        )
    with tempfile.TemporaryDirectory(prefix="canvas-evidence-probe-") as directory:
        path = Path(directory) / "source.mp4"
        await run_media_command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=96x64:r=12:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='gte(t,1)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        )
        data = path.read_bytes()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000/api/v1",
        timeout=60,
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        key = f"video:probe-{uuid4()}"
        source_url = f"/canvas/storage/infinite-canvas.media_files/{key}"
        cleanup = [source_url]
        try:
            uploaded = await client.put(
                source_url + "?revision=0",
                content=data,
                headers={"Content-Type": "video/mp4", "X-Canvas-Kind": "blob"},
            )
            uploaded.raise_for_status()
            response = await client.post(
                "/canvas/video-evidence", json={"key": key, "count": 4, "sampling": "uniform"}
            )
            response.raise_for_status()
            result = response.json()
            cleanup.extend(
                "/canvas/storage/infinite-canvas.image_files/" + frame["storageKey"]
                for frame in result["frames"] + result["sheets"]
            )
            assert [f["at"] for f in result["frames"]] == [0.25, 0.75, 1.25, 1.75]
            for url in cleanup[1:]:
                saved = await client.get(url)
                saved.raise_for_status()
                assert (
                    saved.headers["content-type"] == "image/webp"
                    and len(saved.content) > 0
                )
            assert (await client.get(source_url)).content == data
            response = await client.post('/canvas/video-evidence', json={'key': key, 'count': 4})
            response.raise_for_status()
            adaptive = response.json()
            cleanup.extend('/canvas/storage/infinite-canvas.image_files/' + frame['storageKey'] for frame in adaptive['frames'] + adaptive['sheets'])
            assert adaptive['sampling'] == 'adaptive' and adaptive['transition_count'] == 1
            assert adaptive['transitions'][0]['at'] == 1
            audio_key = None
            for operation, fields in [('extract_audio', {'start': 0.25, 'end': 1.75}),
                                      ('mux_audio', {'audio_start': 0.1, 'offset': 0.3}),
                                      ('trim_video', {'start': 0.5, 'end': 1.5})]:
                response = await client.post('/canvas/media-process', json={'operation': operation, 'video_key': key, **fields,
                    **({'audio_key': audio_key} if operation == 'mux_audio' else {})})
                response.raise_for_status()
                media = response.json()
                media_url = '/canvas/storage/infinite-canvas.media_files/' + media['storageKey']
                cleanup.append(media_url)
                assert len((await client.get(media_url)).content) > 0
                if operation == 'extract_audio':
                    audio_key = media['storageKey']
                    assert abs(media['duration'] - 1.5) < 0.03
                else:
                    assert abs(media['duration'] - (2 if operation == 'mux_audio' else 1)) < 0.03
            assert (await client.get(source_url)).content == data
            print(
                "Live canvas API: adaptive/exact-range evidence, audio extraction, aligned mux, source clip, owned persistence and original preservation passed"
            )
        finally:
            for url in cleanup:
                (await client.delete(url)).raise_for_status()
    print(
        "Disposable probe video, evidence and audio/video derivatives removed; no model inference sent"
    )


if __name__ == "__main__":
    asyncio.run(main())
