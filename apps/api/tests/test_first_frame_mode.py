import asyncio

import pytest

from app.db.models import AIModel, Project, StoryboardShot, StoryboardVersion
from app.db.session import SessionLocal
from app.services.first_frame_policy import planning_contract, supports_first_frame
from app.services.media_gateway import VideoGenerationResult
from app.services.task_worker import process_task, queued_provider_candidates
from app.services.video_continuity import predecessor
from test_combat_pipeline import configured_test_providers, combat_project, task_json
from test_video_continuity import encoded_video, setup_shots


@pytest.mark.parametrize("modes,enabled,maximum,minimum,formats,expected", [
    (["first_frame"], True, 1, 1, ["image/webp"], True),
    (["first_frame"], True, 5, 0, [], True),
    (["multi_shot"], True, 5, 0, ["image/png"], False),
    (["first_last_frame"], True, 2, 2, ["image/png"], False),
    (["first_frame"], False, 5, 0, ["image/png"], False),
    (["first_frame"], True, 0, 0, ["image/png"], False),
    (["first_frame"], True, 5, 2, ["image/png"], False),
    (["first_frame"], True, 5, 0, ["image/gif"], False),
])
def test_capability_gate(modes, enabled, maximum, minimum, formats, expected):
    assert supports_first_frame({"generation_modes": modes, "reference_limits": {
        "image": {"enabled": enabled, "max_count": maximum, "min_count": minimum,
                  "accepted_mime_types": formats}}}) is expected


def test_setting_validates_effective_model_and_partial_updates(client, creator_headers, combat_project):
    endpoint = f"/api/v1/projects/{combat_project}"
    assert client.get(endpoint, headers=creator_headers).json()["first_frame_mode"] is False
    assert client.patch(endpoint, headers=creator_headers, json={"first_frame_mode": True}).status_code == 200
    renamed = client.patch(endpoint, headers=creator_headers, json={"name": "保持首帧设置"})
    assert renamed.json()["first_frame_mode"] is True
    assert client.patch(endpoint, headers=creator_headers, json={"first_frame_mode": None}).status_code == 422

    async def change_capability():
        async with SessionLocal() as session:
            project = await session.get(Project, combat_project)
            model = await session.get(AIModel, project.video_model_id)
            model.capabilities = {**model.capabilities, "generation_modes": ["multi_shot"]}
            await session.commit()
    asyncio.run(change_capability())
    assert client.patch(endpoint, headers=creator_headers, json={"first_frame_mode": True}).status_code == 422
    assert client.patch(endpoint, headers=creator_headers, json={"first_frame_mode": False}).status_code == 200


def test_disabled_mode_releases_waiting_video_without_using_previous_tail(
    client, creator_headers, combat_project, encoded_video,
):
    chapter_id, board_id, ids = setup_shots(client, creator_headers, combat_project)
    endpoint = f"/api/v1/projects/{combat_project}/chapters/{chapter_id}/storyboards/{board_id}"
    queued = client.post(endpoint + "/videos/generate", headers=creator_headers,
                         json={"shot_ids": ids[:2], "only_missing": False})
    assert queued.status_code == 202, queued.text
    tasks = {t["request_payload"]["shot_id"]: t["id"] for t in queued.json()}

    class Gateway:
        calls = 0
        async def submit_video(self, request):
            self.calls += 1
            assert not any(r.get("role") == "first_frame" for r in request.reference_media)
            assert "项目首帧模式：关闭" in request.prompt
            assert "禁止人物滑移" in request.prompt and "不复播邻镜动作" in request.prompt
            return VideoGenerationResult(status="succeeded", video_data=encoded_video.read_bytes(), content_type="video/mp4")

    gateway = Gateway()
    asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway))
    assert gateway.calls == 0
    assert task_json(client, creator_headers, tasks[ids[1]])["request_payload"]["video_continuity_waiting"]
    result = client.patch(f"/api/v1/projects/{combat_project}", headers=creator_headers,
                          json={"first_frame_mode": False})
    assert result.status_code == 200, result.text
    asyncio.run(queued_provider_candidates())
    assert not task_json(client, creator_headers, tasks[ids[1]])["request_payload"].get("video_continuity_waiting")
    asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway))
    result = task_json(client, creator_headers, tasks[ids[1]])
    assert result["status"] == "succeeded", result
    assert gateway.calls == 1 and not result["request_payload"].get("video_continuity")
    assert task_json(client, creator_headers, tasks[ids[0]])["status"] == "queued"


def test_same_scene_does_not_implicitly_enable_dependencies(client, creator_headers, combat_project):
    _, board_id, ids = setup_shots(client, creator_headers, combat_project)

    async def scenario():
        async with SessionLocal() as session:
            board = await session.get(StoryboardVersion, board_id)
            board.content = [{**item, "continuity_group": ""} for item in board.content]
            await session.commit()
            assert await predecessor(session, await session.get(StoryboardShot, ids[1])) is None
    asyncio.run(scenario())


def test_generation_review_and_combat_share_the_project_policy(client, creator_headers, combat_project):
    from app.services.task_worker import claim_task, build_task_runtime_context

    chapter_id, board_id, ids = setup_shots(client, creator_headers, combat_project)
    endpoint = f"/api/v1/projects/{combat_project}/chapters/{chapter_id}/storyboards/{board_id}"
    queued = client.post(endpoint + "/video-prompts/generate", headers=creator_headers,
                         json={"shot_ids": ids[:1], "overwrite": True})
    assert queued.status_code == 202, queued.text
    task_id = queued.json()["id"]

    async def scenario():
        assert await claim_task(task_id)
        for enabled in (True, False):
            async with SessionLocal() as session:
                project = await session.get(Project, combat_project)
                project.first_frame_mode = enabled
                await session.commit()
            for code in ("storyboard-generation", "storyboard-review", "storyboard-repair", "video-prompt-generation"):
                context = await build_task_runtime_context(task_id, prompt_code=code, prompt="设计当前镜头")
                assert ("项目首帧模式：开启" if enabled else "项目首帧模式：关闭") in context.system_prompt_tail
            combat = await build_task_runtime_context(task_id, prompt_code="video-prompt-generation",
                prompt="设计连续剑术交锋", combat_design=True, combat_query="连续剑术交锋")
            assert ("项目首帧模式：开启" if enabled else "项目首帧模式：关闭") in combat.system_prompt_tail

    asyncio.run(scenario())


def test_disabled_storyboard_clears_generated_dependency_groups():
    import json
    from types import SimpleNamespace
    from app.services.storyboard_generation import generate
    from test_storyboard_generation import request, shot

    class Runtime:
        async def run(self, req):
            return SimpleNamespace(final_response=json.dumps({"shots": [
                {**shot(1), "continuity_group": "must-chain"}]}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={},
        save=noop, progress=noop, first_frame_mode=False))
    assert json.loads(result)["shots"][0]["continuity_group"] == ""
    contract = planning_contract(False)
    assert "项目首帧模式：关闭" in contract and "180度轴线" in contract


def test_disabled_mode_repairs_only_prompt_with_explicit_tail_dependency(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.services import task_worker as worker
    from test_storyboard_generation import request

    calls = []

    async def assembled(*args):
        return request()

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(final_response=json.dumps({"shots": [{"shot_id": "shot-2",
                "video_prompt": "使用上一镜尾帧作为首帧" if len(calls) == 1 else "女子立于门左侧，侧面中景，转身推门"}]}))

    monkeypatch.setattr(worker, "runtime_request_from_context", assembled)
    text = asyncio.run(worker.generate_shot_video_prompt(None, "", {"shot_id": "shot-2",
        "order_index": 2, "first_frame_mode": False}, protocol_appendix="", runtime_factory=Runtime))
    assert len(calls) == 2 and "门左侧" in text
    assert "首帧模式关闭" in calls[1].prompt


def test_old_draft_tail_dependency_becomes_a_local_field_repair():
    from app.services.storyboard_assets import draft_timeline_findings
    from app.services.task_worker import StoryboardGenerationPayload
    from test_storyboard_generation import shot

    board = StoryboardGenerationPayload.model_validate({"shots": [shot(1),
        {**shot(2), "action_description": "从上一镜尾帧继续，女子推门"}]})
    findings = draft_timeline_findings(board, first_frame_mode=False)
    assert len(findings) == 1 and findings[0]["shot_indices"] == [2]
    assert findings[0]["fields"] == ["action_description"]
