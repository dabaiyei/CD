import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import storyboard_assets, task_worker


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
