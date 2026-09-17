import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from test_combat_pipeline import configured_test_providers, combat_project, task_json
from app.db.models import Chapter, Project, ScriptVersion, StoryboardVersion, StoryboardShot, VideoClip, AITask, AIModel
from app.db.session import SessionLocal
from app.services.media_gateway import VideoGenerationResult
from app.services.task_worker import process_task, queued_provider_candidates
from app.services.video_concat import run_media_command, media_binary
from app.services.video_continuity import extract_boundary, predecessor


@pytest.fixture
def encoded_video(tmp_path):
    if not media_binary("ffmpeg"):
        pytest.skip("ffmpeg required for real media tests")
    path = tmp_path / "video.mp4"
    asyncio.run(run_media_command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=256x144:rate=24", "-t", "2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)))
    return path


def setup_shots(client, headers, project_id):
    imported = client.post(f"/api/v1/projects/{project_id}/sources/import", headers=headers,
        data={"mode": "novel", "pasted_text": "第一章\n女子从门口走向窗边，然后转场。"}).json()
    chapter_id = imported["chapters"][0]["id"]
    async def create():
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            scope = dict(tenant_id=project.tenant_id, user_id=project.owner_id, project_id=project.id, chapter_id=chapter_id)
            script = ScriptVersion(**scope, version=1, title="接续测试", content="走向窗边", is_active=True)
            session.add(script)
            await session.flush()
            chapter = await session.get(Chapter, chapter_id)
            chapter.active_script_version_id = script.id
            board = StoryboardVersion(**scope, script_version_id=script.id, version=1, is_active=True,
                content=[{"order_index": i, "continuity_group": "room" if i < 3 else "street"} for i in range(1, 4)])
            session.add(board)
            await session.flush()
            shots = []
            for i in range(1, 4):
                shot = StoryboardShot(**scope, storyboard_version_id=board.id, order_index=i,
                    title=f"镜头{i}", scene_description="房间", action_description="固定机位，人物缓步前行",
                    image_prompt="女子走向窗边", video_prompt="女子缓步走向窗边", duration_seconds=5, asset_ids=[])
                session.add(shot)
                shots.append(shot)
            model = await session.get(AIModel, project.video_model_id)
            # First segment can start without a reference; the next segment must use it.
            model.capabilities = {**model.capabilities, "schema_version": 1,
                "generation_modes": ["text_to_video", "first_frame", "multi_shot"],
                "reference_limits": {"image": {"enabled": True, "max_count": 5, "min_count": 0}},
                "supported_durations_seconds": [5], "aspect_ratios": ["16:9"], "resolutions": ["720p", "1080p"]}
            await session.commit()
            return board.id, [shot.id for shot in shots]
    board_id, ids = asyncio.run(create())
    return chapter_id, board_id, ids


def test_video_dependency_tail_frame_parallel_scene_and_invalidation(client, creator_headers, combat_project, encoded_video):
    chapter_id, board_id, ids = setup_shots(client, creator_headers, combat_project)
    endpoint = f"/api/v1/projects/{combat_project}/chapters/{chapter_id}/storyboards/{board_id}"
    queued = client.post(endpoint + "/videos/generate", headers=creator_headers,
        json={"shot_ids": ids, "only_missing": False})
    assert queued.status_code == 202, queued.text
    tasks = {t["request_payload"]["shot_id"]: t["id"] for t in queued.json()}

    class Gateway:
        def __init__(self):
            self.requests = []
        async def submit_video(self, request):
            self.requests.append(request)
            assert "本镜明确的运动限制" in request.prompt
            assert "固定机位，人物缓步前行" in request.prompt
            if len(self.requests) == 3:
                assert request.reference_media[0]["role"] == "first_frame"
                assert "continuity" in request.reference_media[0]["url"]
                assert request.generation_mode == "first_frame"
                assert "上一镜实际末尾画面" in request.prompt
                # Simulate a provider respecting the reference, with a real decodable output.
                from app.services.object_storage import materialize_media_file, object_key_from_media_url
                frame = await materialize_media_file(object_key_from_media_url(request.reference_media[0]["url"]))
                output = encoded_video.parent / "continued.mp4"
                await run_media_command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-loop", "1", "-i", str(frame), "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output))
                return VideoGenerationResult(status="succeeded", video_data=output.read_bytes(), content_type="video/mp4")
            return VideoGenerationResult(status="succeeded", video_data=encoded_video.read_bytes(), content_type="video/mp4")
    gateway = Gateway()
    assert asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway))
    waiting = task_json(client, creator_headers, tasks[ids[1]])
    assert waiting["status"] == "queued", waiting
    assert not gateway.requests
    assert tasks[ids[1]] not in asyncio.run(queued_provider_candidates())
    # Different scene must run while the same-scene continuation is parked.
    asyncio.run(process_task(tasks[ids[2]], gateway_factory=lambda _: gateway))
    assert task_json(client, creator_headers, tasks[ids[2]])["status"] == "succeeded"
    asyncio.run(process_task(tasks[ids[0]], gateway_factory=lambda _: gateway))
    assert task_json(client, creator_headers, tasks[ids[0]])["status"] == "succeeded"
    asyncio.run(queued_provider_candidates())
    asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway))
    result = task_json(client, creator_headers, tasks[ids[1]])
    assert result["status"] == "succeeded", result
    assert result["result_payload"]["seam_check"]["difference"] < 0.1
    async def invalidate():
        from app.services.video_continuity import invalidate_descendants
        async with SessionLocal() as session:
            await invalidate_descendants(session, ids[0], "replacement")
            await session.commit()
            child = await session.get(VideoClip, result["result_payload"]["video_clip_id"])
            assert not child.is_active
    asyncio.run(invalidate())


def test_boundary_rejects_black_tail_and_extracts_video_reference(tmp_path, encoded_video):
    frame, tail = asyncio.run(extract_boundary(encoded_video, tmp_path, video_reference=True))
    assert frame.is_file() and tail.is_file()
    black_dir = tmp_path / "black"
    black_dir.mkdir()
    black_video = black_dir / "black.mp4"
    asyncio.run(run_media_command("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "color=black:size=256x144:rate=24", "-t", "1", "-c:v", "libx264", str(black_video)))
    with pytest.raises(RuntimeError, match="无有效画面"):
        asyncio.run(extract_boundary(black_video, black_dir, video_reference=False))


def test_failed_predecessor_stops_continuation_without_calling_provider(client, creator_headers, combat_project):
    chapter_id, board_id, ids = setup_shots(client, creator_headers, combat_project)
    endpoint = f"/api/v1/projects/{combat_project}/chapters/{chapter_id}/storyboards/{board_id}"
    queued = client.post(endpoint + "/videos/generate", headers=creator_headers,
        json={"shot_ids": ids[:2], "only_missing": False})
    assert queued.status_code == 202, queued.text
    tasks = {t["request_payload"]["shot_id"]: t["id"] for t in queued.json()}
    class Failure:
        calls = 0
        async def submit_video(self, request):
            self.calls += 1
            return VideoGenerationResult(status="failed", error_message="test predecessor failure")
    gateway = Failure()
    asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway))
    assert asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway)) is False
    asyncio.run(process_task(tasks[ids[0]], gateway_factory=lambda _: gateway))
    asyncio.run(queued_provider_candidates())
    asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway))
    result = task_json(client, creator_headers, tasks[ids[1]])
    assert result["status"] == "failed", result
    assert result["result_payload"]["credit_refunded"]
    assert gateway.calls == 1


def test_reference_mapping_keeps_asset_tokens_and_adds_motion_reference():
    from app.services.video_continuity import apply_reference, continuation_prompt
    binding = {"first_frame_url": "/tail.png", "video_url": "/tail.mp4", "previous_action": "落地",
        "current_action": "借势向前"}
    references = [{"type": "image", "url": "/old.png", "token": "<Picture 1>"},
        {"type": "image", "url": "/hero.png", "token": "<Picture 2>"},
        {"type": "audio", "url": "/voice.mp3", "token": "<Audio 1>"}]
    result = apply_reference(binding, references)
    assert result[0]["url"] == "/tail.png"
    assert result[1] == references[1] and result[2] == references[2]
    assert result[3]["token"] == "<Video 1>"
    assert references[0]["url"] == "/old.png"
    assert "不复播或复制音轨" in continuation_prompt(binding)
