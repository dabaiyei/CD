import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from app.services import hypit_bridge
from app.services.agent_runtime import AgentRuntimeResponse
from app.services.task_worker import process_task
from app.services.video_replica import Plan, Shot, split_shots, validate_coverage


def test_plan_coverage_rejects_gaps_overlaps_and_truncation():
    def shot(start, end):
        return Shot(start=start, end=end, observation="参考画面", prompt="目标画面")

    validate_coverage([shot(0, 4), shot(4, 8)], 0, 8)
    for rows in [[shot(0, 4), shot(5, 8)], [shot(0, 4), shot(3, 8)], [shot(0, 4)]]:
        with pytest.raises(ValueError):
            validate_coverage(rows, 0, 8)
    with pytest.raises(ValueError):
        shot(1, 0)
    with pytest.raises(ValueError):
        shot(0, float("inf"))


def test_model_duration_split_preserves_full_timeline():
    shots = [Shot(start=0, end=31, observation="追逐", prompt="连续追逐，不重复动作")]
    divided = split_shots(shots, 12)
    assert len(divided) == 3
    assert all(s.end - s.start <= 12 for s in divided)
    assert "第2/3部分" in divided[1].prompt
    validate_coverage(divided, 0, 31)


@pytest.mark.parametrize("has_audio", [False, True])
def test_analysis_checkpoint_retry_and_account_isolation(
    client, admin_headers, creator_headers, monkeypatch, has_audio
):
    from app.services import replica_transcription

    transcriptions = []

    async def transcribe(source, root, language, progress):
        transcriptions.append(language)
        return {
            "passages": [
                {"text": "第一段台词", "start_seconds": 1, "end_seconds": 2},
                {"text": "第二段台词", "start_seconds": 13, "end_seconds": 14},
            ]
        }

    monkeypatch.setattr(replica_transcription, "transcribe", transcribe)
    monkeypatch.setattr(hypit_bridge, "available", lambda: True)

    async def command(*args, **kwargs):
        if args[:2] == ("media", "probe"):
            return {"duration": 24, "hasVideo": True, "hasAudio": has_audio, "width": 320, "height": 180}
        folder = Path(args[args.index("--to") + 1])
        folder.mkdir()
        for i in range(16):
            Image.new("RGB", (32, 18), "red").save(folder / f"{i:03}.png")
        return {"frames": [{"at": i, "path": str(folder / f"{i:03}.png")} for i in range(16)]}

    monkeypatch.setattr(hypit_bridge, "command", command)
    config = client.get("/api/v1/video-replicas/config", headers=admin_headers).json()
    text = next(m["id"] for m in config["models"] if m["type"] == "text")
    response = client.post(
        "/api/v1/video-replicas",
        headers=admin_headers,
        data={"brief": "改为动画风格", "text_model_id": text},
        files={"file": ("reference.mp4", b"fixture-video", "video/mp4")},
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]

    # Submitted v1 jobs must retain their original checkpoint/batch contract.
    async def legacy_job():
        from app.db.models import AITask
        from app.db.session import SessionLocal

        async with SessionLocal() as db:
            task = await db.get(AITask, task_id)
            task.request_payload = {**task.request_payload, "analysis_version": 1}
            await db.commit()

    asyncio.run(legacy_job())

    class Runtime:
        def __init__(self, fail=False):
            self.calls = []
            self.fail = fail

        async def run(self, request):
            index = int(request.session_id.split("-")[-2])
            self.calls.append(index)
            assert len(request.attachments) == 4
            assert not request.skills and request.tool_mode == "none"
            if has_audio:
                assert ("第一段台词" in request.prompt) == (index == 0)
                assert ("第二段台词" in request.prompt) == (index == 1)
            if index == 1 and self.fail:
                raise RuntimeError("模拟第二段失败")
            return AgentRuntimeResponse(
                session_id=request.session_id,
                final_response=json.dumps(
                    {
                        "summary": "红色画面",
                        "shots": [
                            {
                                "start": index * 12,
                                "end": (index + 1) * 12,
                                "observation": "红色画面",
                                "prompt": "改为动画风格",
                            }
                        ],
                    }
                ),
                finish_reason="completed",
                events=[],
                manifest={},
            )

    first = Runtime(fail=True)
    asyncio.run(process_task(task_id, runtime_factory=lambda: first))
    failed = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert failed["status"] == "failed", failed
    assert list(failed["result_payload"]["batches"]) == ["0"]
    assert client.post(f"/api/v1/tasks/{task_id}/retry", headers=admin_headers).status_code == 202
    second = Runtime()
    asyncio.run(process_task(task_id, runtime_factory=lambda: second))
    done = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert done["status"] == "succeeded", done
    assert second.calls == [1]
    assert transcriptions == (["zh"] if has_audio else [])
    assert len(done["result_payload"]["plan"]["shots"]) == 2
    assert client.get(f"/api/v1/tasks/{task_id}", headers=creator_headers).status_code == 404
    assert not any(
        t["id"] == task_id for t in client.get("/api/v1/video-replicas", headers=creator_headers).json()
    )
    video = next(m["id"] for m in config["models"] if m["type"] == "video")

    async def capable_model():
        from app.db.models import AIModel
        from app.db.session import SessionLocal

        async with SessionLocal() as db:
            base = await db.get(AIModel, video)
            model = AIModel(
                tenant_id=base.tenant_id,
                provider_id=base.provider_id,
                model_type=base.model_type,
                name="Replica creation fixture",
                model_id=f"replica-create-{has_audio}",
                enabled=True,
                capabilities={"durations": [12], "generation_modes": ["text_to_video"]},
            )
            db.add(model)
            await db.commit()
            return model.id

    video = asyncio.run(capable_model())
    options = {
        "plan": done["result_payload"]["plan"],
        "video_model_id": video,
        "use_reference_frame": False,
        "use_reference_video": False,
    }
    assert (
        client.post(
            f"/api/v1/video-replicas/{task_id}/render", headers=creator_headers, json=options
        ).status_code
        == 404
    )
    options["plan"]["shots"][1]["start"] = 15
    assert (
        client.post(
            f"/api/v1/video-replicas/{task_id}/render", headers=admin_headers, json=options
        ).status_code
        == 422
    )
    options["plan"]["shots"][1]["start"] = 12
    created = client.post(f"/api/v1/video-replicas/{task_id}/render", headers=admin_headers, json=options)
    assert created.status_code == 202, created.text
    duplicate = client.post(f"/api/v1/video-replicas/{task_id}/render", headers=admin_headers, json=options)
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == created.json()["id"]


def test_no_arbitrary_markup_in_composition(tmp_path):
    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"fixture")
    hypit_bridge.write_composition(tmp_path, [{"path": clip, "duration": 2}], width=320, height=180)
    source = (tmp_path / "composition.svml").read_text(encoding="utf-8")
    assert 'end="60f"' in source
    assert 'src="./assets/clip-0.mp4"' in source
    assert 'source-audio="content"' in source
    assert "hypit.runtime-local@1" in (tmp_path / "hypit.runtime.json").read_text()


def test_input_rejects_injection_and_incomplete_source(client, admin_headers):
    assert (
        client.post(
            "/api/v1/video-replicas",
            headers=admin_headers,
            data={"brief": "复刻", "text_model_id": "anything"},
        ).status_code
        == 422
    )
    with pytest.raises(ValueError):
        Plan.model_validate({"summary": "x", "shots": [], "shell": "execute this"})


@pytest.mark.parametrize(
    "reference_mode,custom_reference",
    [
        ("text_to_video", False),
        ("first_frame", False),
        ("full_reference", False),
        ("first_frame", True),
        ("full_reference", True),
    ],
)
def test_generation_retry_reuses_completed_clips(
    client, admin_headers, monkeypatch, tmp_path, reference_mode, custom_reference
):
    from sqlalchemy import select

    from app.db.models import AIModel, AITask, ModelType, TaskStatus, User
    from app.db.session import SessionLocal
    from app.services.media_gateway import VideoGenerationResult
    from app.services.object_storage import persist_media_file
    from app.services.video_concat import media_binary, run_media_command
    from app.services.video_replica import RENDER

    if not media_binary("ffmpeg"):
        pytest.skip("FFmpeg required")

    async def setup():
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(User.email == "admin@cineforge.local"))
            base = await session.scalar(select(AIModel).where(AIModel.model_type == ModelType.VIDEO))
            model = AIModel(
                tenant_id=user.tenant_id,
                provider_id=base.provider_id,
                model_type=ModelType.VIDEO,
                name="Replica test video",
                model_id=f"replica-test-{reference_mode}-{custom_reference}",
                enabled=True,
                capabilities={
                    "durations": [2, 4],
                    "generation_modes": [reference_mode],
                    "reference_limits": {
                        "video": {"enabled": True, "max_count": 1},
                        "image": {"enabled": True, "max_count": 1, "accepted_mime_types": ["image/png"]},
                    },
                    "resolutions": ["720p"],
                },
            )
            session.add(model)
            await session.flush()
            from app.core.config import get_settings

            root = (
                get_settings().uploads_root
                / user.tenant_id
                / user.id
                / f"test-replica-{reference_mode}-{custom_reference}"
            )
            root.mkdir(parents=True)
            source = root / "source.mp4"
            await run_media_command(
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=red:s=160x90:r=30:d=4",
                "-c:v",
                "libx264",
                str(source),
            )
            key, _ = await persist_media_file(source, "video/mp4")
            image = root / "hero.png"
            Image.new("RGB", (256, 256), "blue").save(image)
            image_key, image_url = await persist_media_file(image, "image/png")
            task = AITask(
                tenant_id=user.tenant_id,
                user_id=user.id,
                task_type=RENDER,
                model_id=model.id,
                cost=0,
                request_payload={
                    "source_key": key,
                    "has_audio": False,
                    "reference_snapshots": [
                        {
                            "name": "hero",
                            "purpose": "replace hero",
                            "storage_key": image_key,
                            "url": image_url,
                        }
                    ]
                    if custom_reference
                    else [],
                    "options": {
                        "plan": {
                            "summary": "test",
                            "shots": [
                                {
                                    "start": i * 2,
                                    "end": i * 2 + 2,
                                    "observation": "red",
                                    "prompt": f"shot-{i}",
                                }
                                for i in range(2)
                            ],
                        },
                        "video_model_id": model.id,
                        "use_reference_frame": reference_mode == "first_frame" and not custom_reference,
                        "audio": "silent",
                    },
                },
            )
            session.add(task)
            await session.commit()
            return task.id, source.read_bytes()

    task_id, data = asyncio.run(setup())
    from app.services import video_replica

    monkeypatch.setattr(video_replica, "CLIP_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(video_replica, "CLIP_POLL_INTERVAL", 0)
    calls = []
    submitted_keys = []
    polled = []
    poll_failure = reference_mode == "full_reference" and custom_reference
    fail_second = True

    class Gateway:
        async def submit_video(self, request):
            if custom_reference:
                assert request.reference_image_url.startswith("data:image/png;base64,")
                assert "replace hero" in request.prompt
                assert request.reference_media[0]["mime_type"] == "image/png"
            elif reference_mode == "full_reference":
                assert request.reference_image_url is None
                assert request.reference_media[0]["type"] == "video"
            elif reference_mode == "first_frame":
                assert request.reference_image_url.startswith("data:image/png;base64,")
                assert request.reference_media[0]["mime_type"] == "image/png"
            assert request.generation_mode == reference_mode
            index = int(request.idempotency_key.removeprefix(task_id + "-").split("-")[0])
            calls.append(index)
            submitted_keys.append(request.idempotency_key)
            if index == 1 and fail_second:
                if poll_failure:
                    return VideoGenerationResult(status="pending", provider_job_id="existing-provider-job")
                return VideoGenerationResult(status="failed", error_message="temporary failure")
            return VideoGenerationResult(status="succeeded", video_data=data, content_type="video/mp4")

        async def poll_video(self, request, job):
            polled.append(job)
            if fail_second:
                raise RuntimeError("temporary polling error")
            return VideoGenerationResult(status="succeeded", video_data=data, content_type="video/mp4")

    async def fake_command(*args, **kwargs):
        if args[0] in {"build", "status"}:
            return {
                "build": {
                    "id": "test-build",
                    "work": {"outcome": "complete"},
                    "result": {"state": "complete"},
                }
            }
        if args[0] == "get":
            Path(args[args.index("--to") + 1]).write_bytes(data)
        return {}

    import os

    if os.environ.get("HYPIT_RENDER_TEST") != "1":
        monkeypatch.setattr(hypit_bridge, "command", fake_command)
    asyncio.run(process_task(task_id, gateway_factory=lambda _: Gateway()))
    failed = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert failed["status"] == "failed", failed
    assert failed["result_payload"]["clips"]["0"]["key"]
    if poll_failure:
        assert calls == [0, 1]
        assert polled == ["existing-provider-job"] * 3
        assert failed["result_payload"]["clips"]["1"]["provider_job_id"] == "existing-provider-job"
    else:
        assert calls == [0, 1, 1, 1]
        assert len(set(submitted_keys)) == len(submitted_keys)
        assert failed["result_payload"]["clips"]["1"]["submission_generation"] == 3
    assert client.post(f"/api/v1/tasks/{task_id}/retry", headers=admin_headers).status_code == 202
    fail_second = False
    asyncio.run(process_task(task_id, gateway_factory=lambda _: Gateway()))
    done = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers).json()
    assert done["status"] == TaskStatus.SUCCEEDED.value, done
    assert calls == ([0, 1] if poll_failure else [0, 1, 1, 1, 1])
    if poll_failure:
        assert polled == ["existing-provider-job"] * 4
    assert done["result_payload"]["media_url"]
