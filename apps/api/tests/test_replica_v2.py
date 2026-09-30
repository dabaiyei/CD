import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models import AIModel, AITask, ModelType, TaskStatus, User
from app.db.session import SessionLocal
from app.services import hypit_bridge
from app.services.agent_runtime import AgentRuntimeResponse
from app.services.media_gateway import VideoGenerationResult
from app.services.object_storage import materialize_media_file, persist_media_file
from app.services.task_worker import process_task
from app.services.video_concat import media_binary, run_media_command
from app.services.video_replica import ANALYZE


@pytest.mark.parametrize("mode", ["full_reference", "first_frame"])
@pytest.mark.parametrize("continuous", [False, True])
def test_replacement_grouping_provider_inputs_and_untrimmed_output(
    client,
    admin_headers,
    creator_headers,
    tmp_path,
    monkeypatch,
    mode,
    continuous,
):
    if not media_binary("ffmpeg"):
        pytest.skip("FFmpeg required")
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, "PNG")
    upload = client.post(
        "/api/v1/agent/attachments",
        headers=admin_headers,
        files={"file": ("new-hero.png", image.getvalue(), "image/png")},
    ).json()

    async def setup():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            base = await db.scalar(select(AIModel).where(AIModel.model_type == ModelType.VIDEO))
            text = await db.scalar(select(AIModel).where(AIModel.model_type == ModelType.TEXT))
            video = AIModel(
                tenant_id=user.tenant_id,
                provider_id=base.provider_id,
                model_type=ModelType.VIDEO,
                name="v2 video",
                model_id=f"v2-video-{mode}-{continuous}",
                enabled=True,
                capabilities={
                    "durations": [4],
                    "generation_modes": [mode],
                    "reference_limits": {
                        "image": {"enabled": True, "max_count": 2},
                        "video": {"enabled": True, "max_count": 1},
                    },
                },
            )
            picture = AIModel(
                tenant_id=user.tenant_id,
                provider_id=base.provider_id,
                model_type=ModelType.IMAGE,
                name="v2 image",
                model_id=f"v2-image-{mode}-{continuous}",
                enabled=True,
                capabilities={"generation_modes": ["image_to_image"], "resolutions": ["1K"]},
            )
            db.add_all([video, picture])
            await db.flush()
            from app.core.config import get_settings

            source = get_settings().uploads_root / user.tenant_id / user.id / f"v2-{mode}-{continuous}.mp4"
            source.parent.mkdir(parents=True, exist_ok=True)
            await run_media_command(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=red:s=160x90:r=30:d={6 if continuous else 3}",
                "-c:v",
                "libx264",
                str(source),
            )
            key, _ = await persist_media_file(source, "video/mp4")
            generated = tmp_path / "generated.mp4"
            await run_media_command(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=blue:s=160x90:r=30:d=4",
                "-c:v",
                "libx264",
                str(generated),
            )
            plan = {
                "summary": "same action",
                "shots": [
                    {
                        "start": i,
                        "end": i + 1,
                        "observation": "old hero at left",
                        "prompt": "old costume, moving right",
                        "scene_id": "room",
                        "boundary": "continuous" if i == 3 else "auto",
                    }
                    for i in range(6 if continuous else 3)
                ],
            }
            # Explicit three-second continuous chunks exceed one four-second request together.
            if continuous:
                plan["shots"][3]["boundary"] = "continuous"
                plan["shots"][3]["scene_id"] = "room-next-angle"
                for row in plan["shots"][4:]:
                    row["scene_id"] = "room-next-angle"
            analysis = AITask(
                tenant_id=user.tenant_id,
                user_id=user.id,
                task_type=ANALYZE,
                status=TaskStatus.SUCCEEDED,
                cost=0,
                request_payload={"source_key": key, "brief": "replace hero", "text_model_id": text.id},
                result_payload={
                    "media": {"duration": 6 if continuous else 3, "hasAudio": False},
                    "plan": plan,
                },
            )
            db.add(analysis)
            await db.commit()
            return analysis.id, video.id, picture.id, plan, generated.read_bytes()

    analysis, video, picture, plan, generated = asyncio.run(setup())
    options = {
        "plan": plan,
        "video_model_id": video,
        "image_model_id": picture,
        "audio": "silent",
        "references": [
            {
                "kind": "attachment",
                "id": upload["id"],
                "target": "old hero",
                "purpose": "use new face and costume",
                "shot_indices": [],
            }
        ],
    }
    preview_url = f"/api/v1/video-replicas/{analysis}/render-plan"
    assert client.post(preview_url, headers=creator_headers, json=options).status_code == 404
    preview = client.post(preview_url, headers=admin_headers, json=options)
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["units"]) == (2 if continuous else 1)
    assert preview.json()["units"][0]["duration"] == 4
    draft = client.patch(
        f"/api/v1/video-replicas/{analysis}", headers=admin_headers, json={"editor_draft": options}
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["request_payload"]["editor_reference_snapshots"][0]["target"] == "old hero"
    result = client.post(f"/api/v1/video-replicas/{analysis}/render", headers=admin_headers, json=options)
    assert result.status_code == 202, result.text
    task_id = result.json()["id"]
    assert result.json()["request_payload"]["generation_plan"] == preview.json()
    submitted, pictures, directions = [], [], []
    fail_first = continuous
    from app.services import video_replica

    monkeypatch.setattr(video_replica, "CLIP_RETRY_DELAYS", (0, 0))

    class Runtime:
        async def run(self, request):
            directions.append(request)
            assert "old hero" in request.prompt and "use new face and costume" in request.prompt
            assert '【镜头语言】' in request.system_prompt
            assert '未授权重新导演时不套用新运镜' in request.system_prompt
            index = int(request.session_id.split("-")[-2])
            assert len(request.attachments) == (2 if index else 1) and request.tool_mode == "none"
            return AgentRuntimeResponse(
                session_id=request.session_id,
                final_response=json.dumps(
                    {
                        "prompt": "新角色从左向右连续行走，三个机位在同一次生成中自然切换，不重复动作。",
                        "shot_indices": [1, 2, 3] if index == 0 else [4, 5, 6],
                        "start_state": "左侧站立",
                        "end_state": "右侧停步",
                    }
                ),
                finish_reason="completed",
                events=[],
                manifest={},
            )

    class Gateway:
        async def generate_image(self, request):
            pictures.append(request)
            assert len(request.reference_image_urls) == 2  # custom identity + original composition
            assert "old hero" in request.prompt
            return image.getvalue()

        async def submit_video(self, request):
            submitted.append(request)
            index = int(request.idempotency_key.removeprefix(task_id + "-").split("-")[0])
            assert "old costume" not in request.prompt
            assert request.duration_seconds == 4
            assert request.generation_mode == mode
            assert [r["type"] for r in request.reference_media] == (
                (["image", "image", "video"] if index else ["image", "video"])
                if mode == "full_reference"
                else ["image"]
            )
            if index:
                assert "实际尾帧" in request.prompt
            if fail_first:
                assert index == 0  # Dependent unit must never start on an unaccepted predecessor.
                return VideoGenerationResult(status="failed", error_message="transient")
            return VideoGenerationResult(status="succeeded", video_data=generated, content_type="video/mp4")

    async def command(*args, **kwargs):
        if args[0] in {"build", "status"}:
            return {"build": {"id": "test", "work": {"outcome": "complete"}, "result": {"state": "complete"}}}
        if args[0] == "get":
            Path(args[args.index("--to") + 1]).write_bytes(generated)
        return {}

    monkeypatch.setattr(hypit_bridge, "command", command)
    asyncio.run(process_task(task_id, runtime_factory=Runtime, gateway_factory=lambda _: Gateway()))
    if continuous:
        failed = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
        assert failed["status"] == "failed"
        assert len(directions) == 1 and len(submitted) == 3
        fail_first = False
        assert client.post(f"/api/v1/tasks/{task_id}/retry", headers=admin_headers).status_code == 202
        asyncio.run(process_task(task_id, runtime_factory=Runtime, gateway_factory=lambda _: Gateway()))
    done = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert done["status"] == "succeeded", done.get("error_message")
    assert len(submitted) == (5 if continuous else 1)
    assert len(directions) == (2 if continuous else 1)
    assert len(pictures) == (1 if mode == "first_frame" else 0)
    clip = done["result_payload"]["clips"]["0"]
    assert clip["duration"] == 4  # NOT truncated to three original one-second shots
    assert clip["shot_indices"] == [1, 2, 3]

    async def actual_duration():
        path = await materialize_media_file(clip["key"])
        raw = await run_media_command("ffprobe", "-v", "error", "-show_format", "-of", "json", str(path))
        return float(json.loads(raw)["format"]["duration"])

    assert 3.9 < asyncio.run(actual_duration()) < 4.1
