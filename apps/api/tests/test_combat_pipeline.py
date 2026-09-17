import asyncio
import json
import pytest
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
from sqlalchemy import select, update

from app.db.models import AITask, Asset, Chapter, Project, ScriptVersion, StoryboardShot, StoryboardVersion, TaskStatus
from app.db.session import SessionLocal
from app.services.agent_runtime import AgentRuntimeResponse
from app.services.combat_techniques import CombatTechnique
from app.services.shot_first_frames import needs_combat_frame
from app.services.task_worker import process_task, queued_provider_candidates


@pytest.fixture(autouse=True)
def configured_test_providers(client):
    from app.core.security import SecretBox
    from app.db.models import Provider, AIModel, ModelType, CreditAccount
    original = []
    async def configure():
        async with SessionLocal() as session:
            for account in (await session.scalars(select(CreditAccount))).all():
                original.append((CreditAccount, account.id, "balance", account.balance))
            for provider in (await session.scalars(select(Provider))).all():
                original.append((Provider, provider.id, "encrypted_api_key", provider.encrypted_api_key))
                provider.encrypted_api_key = SecretBox().encrypt("test-only-no-real-provider-calls")
            for project in (await session.scalars(select(Project))).all():
                original.append((Project, project.id, "aspect_ratio", project.aspect_ratio))
                project.aspect_ratio = "16:9"
            for model in (await session.scalars(select(AIModel).where(AIModel.model_type == ModelType.VIDEO))).all():
                original.append((AIModel, model.id, "capabilities", model.capabilities))
                model.capabilities = {**model.capabilities, "generation_modes": ["first_frame", "multi_shot"],
                    "reference_limits": {"image": {"enabled": True, "max_count": 5, "min_count": 1}}}
            await session.commit()
    asyncio.run(configure())
    yield
    async def restore():
        async with SessionLocal() as session:
            for model, identity, field, value in original:
                row = await session.get(model, identity)
                if row:
                    setattr(row, field, value)
            await session.commit()
    asyncio.run(restore())


def technique_data():
    return dict(name="苍龙镇海", kind="召唤", signature="剑诀引龙反制", activation="左足前踏，剑尖引出龙形法印",
        action_chain=["右腕提剑，法印从剑锋扩张", "神龙沿剑指方向俯冲", "龙爪格开来袭兵器"],
        impact="地面碎石沿受力方向滚动", recovery="龙影收回剑尖，双脚落稳",
        visual_identity="双角银鳞青龙，青金剑纹", scale_anchor="龙首高于城楼三倍", limitations="施术后需收剑恢复",
        image_prompt="人物持剑立于青龙前，银鳞双角完整，单幅招式设定图")


@pytest.fixture
def combat_project(client, creator_headers, configured_test_providers):
    base = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    async def create():
        async with SessionLocal() as session:
            source = await session.get(Project, base)
            project = Project(tenant_id=source.tenant_id, owner_id=source.owner_id, name="独立战斗测试",
                aspect_ratio="16:9", image_resolution=source.image_resolution, image_model_id=source.image_model_id,
                video_model_id=source.video_model_id, video_resolution=source.video_resolution,
                visual_handbook_id=source.visual_handbook_id, director_handbook_id=source.director_handbook_id)
            session.add(project)
            await session.commit()
            return project.id
    identity = asyncio.run(create())
    yield identity
    async def stop():
        async with SessionLocal() as session:
            await session.execute(update(AITask).where(AITask.project_id == identity, AITask.status.in_([TaskStatus.RUNNING, TaskStatus.QUEUED])).values(status=TaskStatus.CANCELLED))
            await session.commit()
    asyncio.run(stop())
    assert client.delete(f"/api/v1/projects/{identity}", headers=creator_headers).status_code == 204


class DesignRuntime:
    async def run(self, request):
        assert "人物" in request.prompt
        return AgentRuntimeResponse(session_id=request.session_id,
            final_response=json.dumps({"techniques": [technique_data()]}, ensure_ascii=False),
            finish_reason="completed", events=[], manifest={})


@pytest.mark.parametrize("stage", ["script-generation", "script-review", "script-repair",
    "storyboard-generation", "storyboard-review", "storyboard-repair", "video-prompt-generation"])
def test_combat_script_rules_reach_runtime_system_context(client, creator_headers, combat_project, stage):
    from app.services.task_worker import claim_task, runtime_request
    owner = create_character(client, creator_headers, combat_project)
    queued = client.post(f"/api/v1/assets/{owner['id']}/techniques/design", headers=creator_headers,
        json={"brief": "剑术对决", "count": 1})
    assert queued.status_code == 202, queued.text
    task_id = queued.json()["id"]
    assert asyncio.run(claim_task(task_id)) == task_id
    request = asyncio.run(runtime_request(task_id, prompt_code=stage, prompt="两人静立石台两侧，摄影机极缓横移；枪仙沉肩抬枪蓄力，枪尖斜指下段，切至下一镜突刺。"))
    assert "系统战斗专项规则优先于导演手册、画风手册" in request.system_prompt
    if stage == "video-prompt-generation":
        assert "不得二次总结删减" in request.system_prompt
    else:
        assert "战斗模块分工 v2" in request.system_prompt
        assert "不展开逐招" in request.system_prompt
        assert "不得因尚无逐招细节而判失败" in request.system_prompt


class ImageGateway:
    def __init__(self, fail=False):
        self.requests = []
        self.fail = fail

    async def generate_image(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("test provider failure")
        assert request.generation_mode == "image_to_image"
        assert request.reference_image_urls[0].startswith("data:image/")
        assert request.aspect_ratio == "16:9"
        output = BytesIO()
        Image.new("RGB", (256, 144), "navy").save(output, "PNG")
        return output.getvalue()


def task_json(client, headers, task_id):
    return client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()


def create_character(client, headers, project_id):
    result = client.post(f"/api/v1/projects/{project_id}/assets", headers=headers,
        json={"asset_type": "character", "name": "战斗测试人物" + uuid4().hex[:6], "description": "青衣剑客"})
    assert result.status_code == 201, result.text
    asset = result.json()
    output = BytesIO()
    Image.new("RGB", (256, 144), "green").save(output, "PNG")
    uploaded = client.post(f"/api/v1/assets/{asset['id']}/image/upload", headers=headers,
        files={"file": ("hero.png", output.getvalue(), "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()


@pytest.mark.parametrize("owner_has_image", [False, True])
def test_technique_design_persists_owner_memory_and_reference_image(client, creator_headers, admin_headers, combat_project, owner_has_image):
    project_id = combat_project
    owner = create_character(client, creator_headers, project_id)
    endpoint = f"/api/v1/assets/{owner['id']}/techniques/design"
    assert client.post(endpoint, headers=admin_headers, json={"brief": "巨龙", "count": 1}).status_code == 404
    queued = client.post(endpoint, headers=creator_headers, json={"brief": "专属神龙召唤", "count": 1})
    assert queued.status_code == 202, queued.text
    assert client.post(endpoint, headers=creator_headers, json={"brief": "再设计", "count": 1}).status_code == 409
    task_id = queued.json()["id"]
    assert asyncio.run(process_task(task_id, runtime_factory=DesignRuntime))
    task = task_json(client, creator_headers, task_id)
    assert task["status"] == "succeeded", task
    assets = client.get(f"/api/v1/projects/{project_id}/assets", headers=creator_headers).json()
    move = next(a for a in assets if a["id"] in task["result_payload"]["asset_ids"])
    assert move["parent_asset_id"] == owner["id"]
    assert "三倍" in move["description"]
    assert move["asset_metadata"]["combat_technique"]["kind"] == "召唤"
    async def prepare_old_images():
        async with SessionLocal() as session:
            technique = await session.get(Asset, move["id"])
            technique.media_url = owner["media_url"]  # Old images can still contain a caster.
            if not owner_has_image:
                parent = await session.get(Asset, owner["id"])
                parent.media_url = None
            await session.commit()
    asyncio.run(prepare_old_images())
    images = client.post(f"/api/v1/projects/{project_id}/assets/images/generate", headers=creator_headers, json={"asset_ids": [move["id"]]})
    assert images.status_code == 202, images.text
    image_id = images.json()[0]["id"]
    class EffectGateway:
        def __init__(self):
            self.requests = []

        async def generate_image(self, request):
            self.requests.append(request)
            assert request.generation_mode == "text_to_image"
            assert not request.reference_image_urls
            assert not request.reference_image_url
            assert "不画施术者" in request.prompt
            output = BytesIO()
            Image.new("RGB", (256, 144), "blue").save(output, "PNG")
            return output.getvalue()

    gateway = EffectGateway()
    asyncio.run(process_task(image_id, gateway_factory=lambda _: gateway))
    assert task_json(client, creator_headers, image_id)["status"] == "succeeded"
    assert gateway.requests[0].prompt.startswith(move["generation_prompt"])
    assert "不画施术者" in gateway.requests[0].prompt


def test_combat_frame_detection_distinguishes_portrait_from_actual_frame():
    asset = SimpleNamespace(media_url="/portrait.webp", asset_metadata={})
    shot = SimpleNamespace(title="挥剑格挡", scene_description="城楼", action_description="右腕提剑", reference_image_url="/portrait.webp")
    assert needs_combat_frame(shot, [asset])
    shot.reference_image_url = "/frame.webp"
    assert not needs_combat_frame(shot, [asset])
    shot.title = "交谈"
    shot.action_description = "看向城楼"
    shot.reference_image_url = None
    assert not needs_combat_frame(shot, [asset])
    asset.asset_metadata = {"combat_technique": technique_data()}
    assert needs_combat_frame(shot, [asset])


def test_first_frame_stays_first_when_technique_references_are_prioritized():
    from app.db.models import AssetType
    from app.services.video_references import build_shot_image_references

    hero = SimpleNamespace(id="hero", name="剑客", asset_type=AssetType.CHARACTER,
        description="青衣", media_url="/hero.webp", asset_metadata={})
    move = SimpleNamespace(id="move", name="苍龙镇海", asset_type=AssetType.CHARACTER,
        description="银鳞青龙", media_url="/move.webp", asset_metadata={"combat_technique": technique_data()})
    shot = SimpleNamespace(reference_image_url="/frame.webp", asset_ids=["hero", "move"])
    references = build_shot_image_references(shot, {"hero": hero, "move": move})
    assert [r["url"] for r in references] == ["/frame.webp", "/move.webp", "/hero.webp"]
    assert references[0]["role"] == "first_frame"
    assert [r["token"] for r in references] == ["<Picture 1>", "<Picture 2>", "<Picture 3>"]


def test_technique_reference_binds_owner_without_copying_character():
    from app.db.models import AssetType
    from app.services.video_references import build_shot_image_references
    from app.services.task_worker import ensure_video_prompt_reference_locks
    hero = SimpleNamespace(id="hero", name="青衣女子", asset_type=AssetType.CHARACTER,
        description="青衣", media_url="/hero.webp", asset_metadata={})
    move = SimpleNamespace(id="move", name="苍龙镇海", asset_type=AssetType.CHARACTER,
        parent_asset_id="hero", description="银鳞青龙", media_url="/move.webp",
        asset_metadata={"combat_technique": technique_data()})
    shot = SimpleNamespace(reference_image_url=None, asset_ids=["move"])
    refs = build_shot_image_references(shot, {"hero": hero, "move": move})
    assert [r["url"] for r in refs] == ["/hero.webp", "/move.webp"]
    assert refs[1]["role"] == "technique_reference"
    assert refs[1]["owner_name"] == "青衣女子"
    # Tokens already present must not suppress ownership/identity constraints.
    prompt = ensure_video_prompt_reference_locks("<Picture 1> <Picture 2>", refs, language="zh-CN")
    assert "青衣女子所属招式" in prompt
    assert "不复制图中任何人物" in prompt
    assert "释放位置、持握手、目标方向" in prompt


def create_asset_tree(client, headers, project_id):
    ids = []
    for name in ["青衣女侠", "青衣女侠·战损", "青衣女侠·战损近景"]:
        response = client.post(f"/api/v1/projects/{project_id}/assets", headers=headers, json={
            "asset_type": "character", "name": name, "description": "同一位黑发女性剑客",
            "generation_prompt": "黑发女性剑客，青色衣衫",
            "parent_asset_id": ids[0] if ids else None,
        })
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return ids


@pytest.mark.parametrize("fail_parent", [False, True])
def test_derivatives_wait_without_worker_slot_and_receive_parent_bytes(client, creator_headers, combat_project, fail_parent):
    ids = create_asset_tree(client, creator_headers, combat_project)
    response = client.post(f"/api/v1/projects/{combat_project}/assets/images/generate",
        headers=creator_headers, json={"asset_ids": list(reversed(ids))})
    assert response.status_code == 202, response.text
    tasks = {t["request_payload"]["asset_id"]: t["id"] for t in response.json()}
    assert asyncio.run(process_task(tasks[ids[1]])) is False
    assert asyncio.run(process_task(tasks[ids[2]])) is False
    assert tasks[ids[1]] not in asyncio.run(queued_provider_candidates())

    class RootGateway:
        async def generate_image(self, request):
            assert not request.reference_image_urls
            if fail_parent:
                raise RuntimeError("parent image failed")
            output = BytesIO()
            Image.new("RGB", (256, 144), "purple").save(output, "PNG")
            return output.getvalue()

    asyncio.run(process_task(tasks[ids[0]], gateway_factory=lambda _: RootGateway()))
    asyncio.run(queued_provider_candidates())
    gateway = ImageGateway()
    asyncio.run(process_task(tasks[ids[1]], gateway_factory=lambda _: gateway))
    child = task_json(client, creator_headers, tasks[ids[1]])
    if fail_parent:
        assert child["status"] == "failed", child
        assert child["result_payload"]["credit_refunded"]
        assert not gateway.requests
    else:
        assert child["status"] == "succeeded", child
        assert child["result_payload"]["parent_reference"]["asset_id"] == ids[0]
        import base64
        from app.services.object_storage import object_storage, object_key_from_media_url
        parent_result = task_json(client, creator_headers, tasks[ids[0]])["result_payload"]
        stored = asyncio.run(object_storage().get_bytes(object_key_from_media_url(parent_result["media_url"])))
        assert base64.b64decode(gateway.requests[0].reference_image_urls[0].split(",", 1)[1]) == stored
    asyncio.run(queued_provider_candidates())
    asyncio.run(process_task(tasks[ids[2]], gateway_factory=lambda _: gateway))
    grandchild = task_json(client, creator_headers, tasks[ids[2]])
    assert grandchild["status"] == ("failed" if fail_parent else "succeeded"), grandchild


def test_missing_parent_rejected_before_charge_and_prompt_image_exclusion(client, creator_headers, combat_project):
    ids = create_asset_tree(client, creator_headers, combat_project)
    base = f"/api/v1/projects/{combat_project}/assets"
    response = client.post(base + "/images/generate", headers=creator_headers, json={"asset_ids": [ids[1]]})
    assert response.status_code == 409, response.text
    response = client.post(base + "/prompts/generate", headers=creator_headers, json={"asset_ids": [ids[0]]})
    assert response.status_code == 202, response.text
    response = client.post(base + "/images/generate", headers=creator_headers, json={"asset_ids": [ids[0]]})
    assert response.status_code == 409, response.text


def test_parent_edit_during_derivative_generation_discards_stale_result(client, creator_headers, combat_project):
    owner = create_character(client, creator_headers, combat_project)
    response = client.post(f"/api/v1/projects/{combat_project}/assets", headers=creator_headers, json={
        "asset_type": "character", "name": "战损", "description": "衣服破损",
        "parent_asset_id": owner["id"], "generation_prompt": "保留主图人物，衣服破损",
    })
    assert response.status_code == 201, response.text
    child_id = response.json()["id"]
    queued = client.post(f"/api/v1/projects/{combat_project}/assets/images/generate",
        headers=creator_headers, json={"asset_ids": [child_id]})
    assert queued.status_code == 202, queued.text
    task_id = queued.json()[0]["id"]

    class EditingGateway(ImageGateway):
        async def generate_image(self, request):
            async with SessionLocal() as session:
                parent = await session.get(Asset, owner["id"])
                parent.version += 1
                await session.commit()
            return await super().generate_image(request)

    asyncio.run(process_task(task_id, gateway_factory=lambda _: EditingGateway()))
    result = task_json(client, creator_headers, task_id)
    assert result["status"] == "failed", result
    assert result["result_payload"]["credit_refunded"]
    assets = client.get(f"/api/v1/projects/{combat_project}/assets", headers=creator_headers).json()
    assert not next(a for a in assets if a["id"] == child_id)["media_url"]


def test_derivative_prompt_receives_parent_identity_and_auto_queues_images(client, creator_headers, combat_project):
    ids = create_asset_tree(client, creator_headers, combat_project)

    async def queue():
        from app.db.models import User
        from app.services.asset_tasks import queue_asset_prompt_generation_task
        async with SessionLocal() as session:
            project = await session.get(Project, combat_project)
            user = await session.get(User, project.owner_id)
            assets = list((await session.scalars(select(Asset).where(Asset.id.in_(ids)))).all())
            task, _, _ = await queue_asset_prompt_generation_task(session, user=user, project=project,
                assets=assets, auto_queue_images_after_prompt=True)
            await session.commit()
            return task.id

    class PromptRuntime:
        async def run(self, request):
            assert '"parent_identity"' in request.prompt
            assert "同一位黑发女性剑客" in request.prompt
            return AgentRuntimeResponse(session_id=request.session_id, finish_reason="completed", events=[], manifest={},
                final_response=json.dumps({"assets": [{"asset_id": aid,
                    "generation_prompt": "同一位黑发女性剑客，继承主图五官，青衣"} for aid in ids]}, ensure_ascii=False))

    task_id = asyncio.run(queue())
    asyncio.run(process_task(task_id, runtime_factory=PromptRuntime))
    result = task_json(client, creator_headers, task_id)
    assert result["status"] == "succeeded", result
    assert result["result_payload"]["image_queue_error"] is None
    assert len(result["result_payload"]["queued_image_task_ids"]) == 3
    queued = [task_json(client, creator_headers, tid) for tid in result["result_payload"]["queued_image_task_ids"]]
    assert sum(bool(t["request_payload"].get("asset_parent_waiting")) for t in queued) == 2
    response = client.post(f"/api/v1/projects/{combat_project}/assets/prompts/generate",
        headers=creator_headers, json={"asset_ids": [ids[0]]})
    assert response.status_code == 409


@pytest.mark.parametrize("fail_after_design", [False, True])
def test_combat_prompts_use_assets_without_independent_frames(client, creator_headers, combat_project, monkeypatch, fail_after_design):
    project_id = combat_project
    owner = create_character(client, creator_headers, project_id)
    imported = client.post(f"/api/v1/projects/{project_id}/sources/import", headers=creator_headers,
        data={"mode": "novel", "pasted_text": "第一章 战斗\n剑客格挡来袭兵器。"}).json()
    chapter_id = imported["chapters"][0]["id"]

    async def setup():
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            scope = dict(tenant_id=project.tenant_id, user_id=project.owner_id, project_id=project.id, chapter_id=chapter_id)
            script = ScriptVersion(**scope, version=1, title="战斗", content="剑客挥剑", is_active=True)
            session.add(script)
            await session.flush()
            chapter = await session.get(Chapter, chapter_id)
            chapter.active_script_version_id = script.id
            board = StoryboardVersion(**scope, script_version_id=script.id, version=1)
            session.add(board)
            await session.flush()
            shot = StoryboardShot(**scope, storyboard_version_id=board.id, order_index=1,
                title="挥剑格挡", action_description="右足蹬地，抬剑格挡，剑锋擦出火花后收势", image_prompt="青衣剑客持剑起势，城楼前",
                asset_ids=[owner["id"]], reference_image_url=owner["media_url"])
            session.add(shot)
            await session.commit()
            return board.id, shot.id

    board_id, shot_id = asyncio.run(setup())
    path = f"/api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{board_id}/video-prompts/generate"
    result = client.post(path, headers=creator_headers, json={"shot_ids": [shot_id]})
    assert result.status_code == 202, result.text
    parent_id = result.json()["id"]
    if fail_after_design:
        from app.services import task_worker
        original_policy = task_worker.enforce_video_audio_policy
        failed = False
        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulate failure after persisted choreography")
            return original_policy(*args, **kwargs)
        monkeypatch.setattr(task_worker, "enforce_video_audio_policy", fail_once)
    class VideoPromptRuntime:
        calls = 0
        async def run(self, request):
            self.calls += 1
            from app.services.combat_choreography import DESIGN_RULES
            assert (request.system_prompt + request.prompt).count(DESIGN_RULES) == 1
            assert len(request.system_prompt) + len(request.prompt) <= 32000
            assert request.memory_context == []
            assert "<martial-reference" in request.prompt
            assert 'name="sword.md"' in request.prompt
            assert 'name="grappling.md"' not in request.prompt
            assert owner["media_url"] in request.prompt
            assert '"role": "asset_reference"' in request.prompt
            assert '"role": "first_frame"' not in request.prompt
            from test_combat_choreography import design_payload
            assert "-combat-" in request.session_id
            if fail_after_design and self.calls == 1:
                return AgentRuntimeResponse(session_id=request.session_id, final_response="not JSON",
                    finish_reason="completed", events=[], manifest={})
            return AgentRuntimeResponse(session_id=request.session_id, final_response=json.dumps(design_payload(), ensure_ascii=False),
                finish_reason="completed", events=[], manifest={})
    runtime = VideoPromptRuntime()
    asyncio.run(process_task(parent_id, runtime_factory=lambda: runtime))
    if fail_after_design:
        failed_task = task_json(client, creator_headers, parent_id)
        assert failed_task["status"] == "failed"
        assert failed_task["request_payload"]["combat_designs"][shot_id]["design"]["beats"]
        retry = client.post(f"/api/v1/tasks/{parent_id}/retry", headers=creator_headers)
        assert retry.status_code == 202, retry.text
        asyncio.run(process_task(parent_id, runtime_factory=lambda: runtime))
    finished = task_json(client, creator_headers, parent_id)
    assert finished["status"] == "succeeded", finished
    assert runtime.calls == (2 if fail_after_design else 1)  # Only malformed design is regenerated.
    async def check_saved_actions():
        from test_combat_choreography import design_payload
        async with SessionLocal() as session:
            shot = await session.get(StoryboardShot, shot_id)
            board = await session.get(StoryboardVersion, board_id)
            for action in design_payload()["beats"][0]["actions"]:
                assert action in shot.video_prompt
            assert board.content[0]["combat_design"]["version"] == "2"
            assert "sword.md" in board.content[0]["combat_design"]["skill_retrieval"]["selected"]
    asyncio.run(check_saved_actions())
    assert not finished["request_payload"].get("first_frame_task_ids")
