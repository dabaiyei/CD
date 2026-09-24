import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import storyboard_assets, task_worker


def test_repair_keeps_bindings_on_dialogue_change_and_reindex():
    original = task_worker.GeneratedStoryboardShotPayload(title="旧镜", image_prompt="首帧",
        scene_description="雨衣", asset_names=["林遥·雨衣", "林遥"])
    previous = [original.model_dump(mode="json")]
    inserted = original.model_copy(update={"scene_description": "战甲", "asset_names": ["林遥·战甲"]})
    changed, preserved = storyboard_assets.repair_asset_rows(
        [inserted, original.model_copy(update={"dialogue": "新台词"})], previous)
    assert [row["order_index"] for row in changed] == [1]
    assert preserved == {2: ["林遥·雨衣", "林遥"]}


def test_nonvisual_repair_never_calls_asset_runtime():
    original = task_worker.GeneratedStoryboardShotPayload(title="旧镜", image_prompt="首帧", asset_names=["角色"])
    definitions, bindings = asyncio.run(storyboard_assets.plan("no-task", [original],
        lambda: pytest.fail("不应再次提取资产"), previous_shots=[original.model_dump(mode="json")]))
    assert definitions == []
    assert bindings == {1: ["角色"]}


def test_explicit_binding_prune_is_not_undone_by_parent_expansion():
    original = task_worker.GeneratedStoryboardShotPayload(title="原镜", image_prompt="首帧",
        asset_names=["角色", "角色·雨衣"])
    changed, preserved = storyboard_assets.repair_asset_rows(
        [original.model_copy(update={"asset_names": ["角色·雨衣"]})], [original.model_dump(mode="json")])
    assert changed == []
    assert preserved == {1: ["角色·雨衣"]}


def test_sequential_batches_reuse_prior_and_resume(monkeypatch):
    task = SimpleNamespace(request_payload={}, project_id="p", tenant_id="t", user_id="u")
    commits = []

    class Session:
        async def commit(self):
            commits.append(json.loads(json.dumps(task.request_payload)))

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def owned(*args):
        return task

    async def catalog(*args):
        return [SimpleNamespace(id="root", name="林遥", asset_type=SimpleNamespace(value="character"),
            parent_asset_id=None, description="短发女子")]

    async def request(*args, **kwargs):
        return SimpleNamespace(prompt=kwargs["prompt"], system_prompt="", session_id="test")

    async def progress(*args):
        pass

    monkeypatch.setattr(task_worker, "SessionLocal", sessions)
    monkeypatch.setattr(task_worker, "owned_task_for_update", owned)
    monkeypatch.setattr(task_worker, "owns_running_task", lambda _: True)
    monkeypatch.setattr(task_worker, "runtime_request", request)
    monkeypatch.setattr(task_worker, "record_progress", progress)
    monkeypatch.setattr(storyboard_assets, "extraction_asset_catalog", catalog)
    monkeypatch.setattr(storyboard_assets, "batches", lambda rows, budget: [[row] for row in rows])
    shots = [task_worker.GeneratedStoryboardShotPayload(title=str(i), image_prompt="首帧",
        scene_description="雨衣造型", asset_names=["林遥"]) for i in range(2)]
    variant = dict(asset_type="character", name="林遥·雨衣", parent_name="林遥", description="同一女子穿雨衣")

    class Runtime:
        fail = True
        calls = []

        async def run(self, request):
            rows = json.loads(request.prompt.split("本批完整分镜：")[1])
            index = rows[0]["order_index"]
            self.calls.append(index)
            if index == 2:
                prior = json.loads(request.prompt.split("前批已确认资产：")[1].split("\n本批完整分镜：")[0])
                assert prior[0]["name"] == "林遥·雨衣"
                assert commits[0]["storyboard_asset_batches"]["completed"]
                if self.fail:
                    raise RuntimeError("temporary provider failure")
            return SimpleNamespace(final_response=json.dumps({"assets": [variant] if index == 1 else [],
                "shots": [{"order_index": index, "asset_names": ["林遥", "林遥·雨衣"]}]}))

    runtime = Runtime()
    with pytest.raises(RuntimeError, match="temporary"):
        asyncio.run(storyboard_assets.plan("task", shots, lambda: runtime))
    runtime.fail = False
    definitions, bindings = asyncio.run(storyboard_assets.plan("task", shots, lambda: runtime))
    assert runtime.calls == [1, 2, 2]
    assert len(definitions) == 1
    assert bindings[1] == bindings[2] == ["林遥", "林遥·雨衣"]


def test_batch_budget_never_splits_one_shot():
    rows = [{"order_index": i, "action": "动作" * 30} for i in range(3)]
    assert storyboard_assets.batches(rows, 120) == [[row] for row in rows]
    with pytest.raises(RuntimeError, match="单个分镜"):
        storyboard_assets.batches(rows, 20)


def test_automatic_pipeline_creates_derivative_only_after_storyboard(client, creator_headers, admin_headers, monkeypatch):
    import test_api
    from app.services.agent_runtime import AgentRuntimeResponse
    from app.db.models import Asset, StoryboardShot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    original = test_api.FakeAutomaticDirectorRuntime
    state = {}

    class Runtime(original):
        async def run(self, request):
            if "本批完整分镜：" not in request.prompt:
                return await super().run(request)
            rows = json.loads(request.prompt.split("本批完整分镜：", 1)[1])
            catalog = json.loads(request.prompt.split("项目目录：", 1)[1].split("\n前批已确认资产：", 1)[0])
            assert not any(a["parent_name"] for a in catalog)
            state["extracted"] = True
            data = {"assets": [{"asset_type": "character", "name": "林遥·雨衣测试",
                "parent_name": "林遥", "description": "沿用林遥的短发女性身份，新增深色雨衣"}],
                "shots": [{"order_index": row["order_index"],
                    "asset_names": list(dict.fromkeys(row["asset_names"] + ["林遥", "林遥·雨衣测试"]))} for row in rows]}
            return AgentRuntimeResponse(session_id=request.session_id, final_response=json.dumps(data),
                finish_reason="completed", events=[], manifest={})

    monkeypatch.setattr(test_api, "FakeAutomaticDirectorRuntime", Runtime)
    test_api.test_automatic_director_workflow_completes_chapter_to_ready_videos(client, creator_headers, admin_headers)
    assert state["extracted"]

    async def verify():
        async with SessionLocal() as session:
            asset = await session.scalar(select(Asset).where(Asset.name == "林遥·雨衣测试"))
            assert asset and asset.parent_asset_id and asset.media_url
            shots = (await session.scalars(select(StoryboardShot).where(StoryboardShot.project_id == asset.project_id))).all()
            assert any(asset.id in shot.asset_ids for shot in shots)
    asyncio.run(verify())


def test_storyboard_draft_cache_preserves_guidance_and_invalidates_on_rule_change(monkeypatch):
    from app.services.agent_runtime import AgentRuntimeRequest

    task = SimpleNamespace(request_payload={})
    guidance = ["当前画风与导演手册：保持角色与表情"]
    calls = []

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def owned(*args):
        return task

    async def runtime_request(*args, **kwargs):
        return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
            prompt=guidance[0] + "\n\n" + kwargs["prompt"], system_prompt="总规则",
            model_binding={}, prompt_versions={}, skill_versions={}, skills=[], memory_context=[])

    async def noop(*args):
        pass

    class Runtime:
        async def run(self, req):
            calls.append(req)
            assert guidance[0] in req.system_prompt
            return SimpleNamespace(final_response=json.dumps({"shots": [{"title": "镜头",
                "duration_seconds": 8, "image_prompt": "保持首帧", "action_description": "转身"}]}), manifest={})

    monkeypatch.setattr(task_worker, "SessionLocal", sessions)
    monkeypatch.setattr(task_worker, "owned_task_for_update", owned)
    monkeypatch.setattr(task_worker, "owns_running_task", lambda _: True)
    monkeypatch.setattr(task_worker, "runtime_request", runtime_request)
    monkeypatch.setattr(task_worker, "record_progress", noop)

    def run():
        return asyncio.run(storyboard_assets.storyboard_response("task", "storyboard-generation",
            "生成分镜\n剧本正文：\n场1｜夜晚\n人物转身", Runtime,
            timing_plan=[], durations=[8], script="场1｜夜晚\n人物转身"))

    first = run()
    second = run()
    assert first.final_response == second.final_response and len(calls) == 1
    guidance[0] = "更新的手册：自然光"
    run()
    assert len(calls) == 2


def test_finished_legacy_board_is_preserved_during_asset_extraction_resume():
    row = {"title": "已完成分镜", "duration_seconds": 8, "image_prompt": "首帧"}
    state = {"valid": {"1": row}, "raw": {}, "segment_shots": {"1": ["1"]}}
    checkpoint = {"key": "old-protocol-key", "state": state}
    draft = {"key": "old-protocol-key", "response": json.dumps({"shots": [row]})}
    assert storyboard_assets._legacy_complete_draft(draft, checkpoint, [8])
    assert not storyboard_assets._legacy_complete_draft(draft, checkpoint, [4])
    assert not storyboard_assets._legacy_complete_draft({**draft, "key": "other-input"}, checkpoint, [8])
    state["segments"] = []
    assert not storyboard_assets._legacy_complete_draft(draft, checkpoint, [8])


def test_legacy_expression_overrun_patches_only_bad_shot_and_preserves_asset_batches(monkeypatch):
    from app.services.agent_runtime import AgentRuntimeRequest

    beat = {"start": 0, "end": 12, "character": "女主", "emotion": "平静克制"}
    rows = [{"title": f"镜{i}", "duration_seconds": 8, "image_prompt": f"首帧{i}",
             "emotion_plan": {"beats": [beat]} if i == 1 else None} for i in (1, 2)]
    board = task_worker.StoryboardGenerationPayload.model_validate({"shots": rows})
    original = board.model_dump(mode="json")["shots"]
    completed = [{"assets": [], "shots": [{"order_index": 1, "asset_names": []},
                                          {"order_index": 2, "asset_names": []}]}]
    task = SimpleNamespace(request_payload={
        "storyboard_draft": {"key": "legacy", "response": board.model_dump_json(), "manifest": {}},
        "storyboard_generation": {"key": "legacy", "state": {"valid": {str(i): r for i, r in enumerate(original, 1)}}},
        "storyboard_asset_batches": {"fingerprint": storyboard_assets._asset_fingerprint(board.shots),
                                     "catalog": [], "completed": completed},
    })

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def owned(*args):
        return task

    async def request(*args, **kwargs):
        return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
            prompt=kwargs["prompt"], system_prompt="保留手册", model_binding={}, prompt_versions={},
            skill_versions={}, skills=[], memory_context=[])

    async def noop(*args):
        pass

    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            assert "只修复第 1 镜" in req.prompt
            return SimpleNamespace(final_response=json.dumps({"fields": {"emotion_plan": {
                "beats": [{**beat, "end": 8}]}}}), manifest={})

    monkeypatch.setattr(task_worker, "SessionLocal", sessions)
    monkeypatch.setattr(task_worker, "owned_task_for_update", owned)
    monkeypatch.setattr(task_worker, "owns_running_task", lambda _: True)
    monkeypatch.setattr(task_worker, "runtime_request", request)
    monkeypatch.setattr(task_worker, "record_progress", noop)
    result = asyncio.run(storyboard_assets.storyboard_response("task", "storyboard-repair", "业务输入",
        Runtime, durations=[8], repair_shots=original))
    repaired = task_worker.StoryboardGenerationPayload.model_validate_json(result.final_response)
    assert len(calls) == 1 and repaired.shots[1] == board.shots[1]
    assert repaired.shots[0].image_prompt == board.shots[0].image_prompt
    assert repaired.shots[0].emotion_plan.beats[0].end == 8
    cache = task.request_payload["storyboard_asset_batches"]
    assert cache["completed"] == completed
    assert cache["fingerprint"] == storyboard_assets._asset_fingerprint(repaired.shots)
    assert not storyboard_assets.draft_timeline_findings(repaired)
