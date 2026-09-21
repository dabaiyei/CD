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
from app.services.combat_choreography import VERSION
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
            assert board.content[0]["combat_design"]["version"] == VERSION
            assert "sword.md" in board.content[0]["combat_design"]["skill_retrieval"]["selected"]
    asyncio.run(check_saved_actions())
    assert not finished["request_payload"].get("first_frame_task_ids")


def _frame_shot(action, reference, title="龙爪压落"):
    from types import SimpleNamespace
    return SimpleNamespace(title=title, scene_description="云海孤峰",
                           action_description=action, reference_image_url=reference)


def _frame_asset(asset_id, media_url):
    from types import SimpleNamespace
    return SimpleNamespace(id=asset_id, media_url=media_url, asset_metadata={})


ASSET_ID = "9ff5aa40-cf4e-4b65-91bf-7b22f337c85b"
CURRENT_PORTRAIT = f"/uploads/t/projects/p/assets/{ASSET_ID}-42e686ae-dc1f-4955-92c0-14fd475d9bcb.webp"
STALE_PORTRAIT = f"/uploads/t/projects/p/assets/{ASSET_ID}-15f1e173-29e8-48cd-a705-fed8d30efb6f.webp"
DESIGNED_FRAME = "/uploads/t/projects/p/agent-attachments/a1b2c3d4-frame.webp"
PORTRAIT_ASSETS = [_frame_asset(ASSET_ID, CURRENT_PORTRAIT)]


def test_regenerated_asset_portrait_still_requires_a_combat_frame():
    # Regenerating an asset keeps its id but changes the file name. Comparing the
    # shot's reference URL by exact string then read the stale portrait as an
    # already-designed first frame, so combat shots rendered from a character
    # sheet and the subject never faced the opponent.
    from app.services.shot_first_frames import needs_combat_frame

    assert needs_combat_frame(_frame_shot("龙爪自云隙压落", STALE_PORTRAIT), PORTRAIT_ASSETS) is True
    assert needs_combat_frame(_frame_shot("龙爪自云隙压落", CURRENT_PORTRAIT), PORTRAIT_ASSETS) is True
    assert needs_combat_frame(_frame_shot("龙爪自云隙压落", None), PORTRAIT_ASSETS) is True
    # A first frame the platform actually designed must not be regenerated.
    assert needs_combat_frame(_frame_shot("龙爪自云隙压落", DESIGNED_FRAME), PORTRAIT_ASSETS) is False


def test_non_combat_shot_never_requests_a_combat_frame():
    from app.services.shot_first_frames import needs_combat_frame

    assert needs_combat_frame(_frame_shot("她静立于岩台边缘眺望", STALE_PORTRAIT, title="晨雾"),
                              PORTRAIT_ASSETS) is False
    assert needs_combat_frame(_frame_shot("她静立于岩台边缘眺望", None, title="晨雾"),
                              PORTRAIT_ASSETS) is False


def test_technique_asset_triggers_a_combat_frame_without_keyword_match():
    from app.services.shot_first_frames import needs_combat_frame

    technique = _frame_asset("tc", "/x.webp")
    technique.asset_metadata = {"combat_technique": {"kind": "剑气"}}
    assert needs_combat_frame(_frame_shot("她静立不动", STALE_PORTRAIT, title="晨雾"),
                              [*PORTRAIT_ASSETS, technique]) is True


@pytest.mark.parametrize("url,expected", [
    (CURRENT_PORTRAIT, ASSET_ID),
    (STALE_PORTRAIT, ASSET_ID),
    (DESIGNED_FRAME, ""),
    ("/uploads/t/projects/p/assets/notauuid-x.webp", ""),
    (None, ""),
    ("", ""),
])
def test_portrait_detection_reads_the_asset_id_not_the_file_name(url, expected):
    from app.services.shot_first_frames import _portrait_asset_id

    assert _portrait_asset_id(url) == expected


def _runtime_context(**overrides):
    from app.services.task_worker import TaskRuntimeContext
    values = dict(tenant_id="t", project_id="p", task_id="task-1",
                  system_prompt_head="HEAD", system_prompt_tail="TAIL", skill_context="SKILLS",
                  model_binding={"model": "m"}, prompt_versions={}, skill_versions={}, skills=[],
                  template_content="TEMPLATE", memory_enabled=False, memory_user_id="u")
    values.update(overrides)
    return TaskRuntimeContext(**values)


def test_shared_context_assembles_the_original_prompt_order():
    # The shot loop builds one task-level context and reuses it; the per-shot
    # request must keep the original section order exactly.
    from app.services.task_worker import assemble_runtime_request

    request = assemble_runtime_request(_runtime_context(), prompt="SHOT", prompt_appendix="APPENDIX")
    assert request.prompt == "TEMPLATE\n\nAPPENDIX\n\nSKILLS\n\nSHOT"
    assert request.system_prompt == "HEADTAIL"
    # With no appendix the template must still precede the skills.
    assert assemble_runtime_request(_runtime_context(), prompt="SHOT").prompt == "TEMPLATE\n\nSKILLS\n\nSHOT"


def test_per_shot_combat_guidance_splices_between_the_task_level_halves():
    from app.services.task_worker import assemble_runtime_request

    request = assemble_runtime_request(_runtime_context(), prompt="SHOT", combat_guidance="\nGUIDE")
    assert request.system_prompt == "HEAD\nGUIDETAIL"


def test_shot_prompt_parses_both_supported_reply_shapes():
    from app.services.task_worker import parse_shot_video_prompt

    row = {"shot_id": "shot-1", "order_index": 3}
    by_id = json.dumps({"shots": [{"shot_id": "shot-1", "video_prompt": " 提示词A "}]})
    assert parse_shot_video_prompt(by_id, row) == "提示词A"
    by_index = json.dumps({"prompts": [{"shot_order_index": 3, "prompt": " 提示词B "}]})
    assert parse_shot_video_prompt(by_index, row) == "提示词B"


@pytest.mark.parametrize("payload,message", [
    ({"shots": [{"shot_id": "other", "video_prompt": "x"}]}, "镜头 ID 不匹配"),
    ({"prompts": [{"shot_order_index": 99, "prompt": "x"}]}, "镜头序号不匹配"),
    ({"shots": [{"shot_id": "shot-1", "video_prompt": "   "}]}, "视频提示词为空"),
])
def test_shot_prompt_rejects_a_mismatched_or_empty_reply(payload, message):
    from app.services.task_worker import parse_shot_video_prompt

    with pytest.raises(RuntimeError, match=message):
        parse_shot_video_prompt(json.dumps(payload), {"shot_id": "shot-1", "order_index": 3})


def test_non_combat_shot_retries_invalid_model_output(monkeypatch):
    # The combat path always retried three times; without the same tolerance a
    # single malformed reply failed the shot, which is most of any storyboard.
    from app.services import task_worker as worker

    calls = []

    class Reply:
        finish_reason = "completed"
        manifest = {}

        def __init__(self, body):
            self.final_response = body

    class Runtime:
        async def run(self, request):
            calls.append(request)
            if len(calls) == 1:
                return Reply("不是 JSON")
            return Reply(json.dumps({"shots": [{"shot_id": "shot-1", "video_prompt": "最终提示词"}]}))

    async def fake_request(context, base_prompt, row, appendix):
        prompt = base_prompt + json.dumps([row], ensure_ascii=False)
        return worker.assemble_runtime_request(context, prompt=prompt)

    monkeypatch.setattr(worker, "runtime_request_from_context", fake_request)
    text = asyncio.run(worker.generate_shot_video_prompt(
        _runtime_context(), "BASE", {"shot_id": "shot-1", "order_index": 1},
        protocol_appendix="", runtime_factory=Runtime))
    assert text == "最终提示词"
    assert len(calls) == 2
    assert "请修正" in calls[1].prompt          # the failure is fed back
    assert calls[1].session_id.endswith("-retry-1")  # and it is a fresh session


def test_non_combat_shot_gives_up_after_three_attempts(monkeypatch):
    from app.services import task_worker as worker

    calls = []

    class Reply:
        finish_reason = "completed"
        manifest = {}
        final_response = "坏回复"

    class Runtime:
        async def run(self, request):
            calls.append(request)
            return Reply()

    async def fake_request(context, base_prompt, row, appendix):
        return worker.assemble_runtime_request(context, prompt=base_prompt)

    monkeypatch.setattr(worker, "runtime_request_from_context", fake_request)
    with pytest.raises(RuntimeError, match="连续 3 次未通过校验"):
        asyncio.run(worker.generate_shot_video_prompt(
            _runtime_context(), "BASE", {"shot_id": "s", "order_index": 2},
            protocol_appendix="", runtime_factory=Runtime))
    assert len(calls) == 3


def _budget_project(preferences):
    from types import SimpleNamespace
    return SimpleNamespace(creation_state={"preferences": preferences})


def test_chapter_budget_is_read_only_from_a_valid_preference():
    from app.services.task_worker import chapter_duration_budget

    assert chapter_duration_budget(_budget_project({"chapter_duration_seconds": 30})) == 30
    # A missing, zeroed or malformed preference means "no budget", not "zero budget",
    # so such a chapter must not have every board rejected.
    for preferences in ({}, {"chapter_duration_seconds": 0},
                        {"chapter_duration_seconds": "30"},
                        {"chapter_duration_seconds": True},
                        {"chapter_duration_seconds": -5}):
        assert chapter_duration_budget(_budget_project(preferences)) is None
    assert chapter_duration_budget(SimpleNamespace(creation_state=None)) is None


def test_board_over_the_chapter_budget_is_rejected_with_the_real_total():
    from app.services.task_worker import validate_duration_budget

    def shots(*durations):
        from types import SimpleNamespace
        return [SimpleNamespace(duration_seconds=value) for value in durations]

    # 30-second chapters were producing several minutes of storyboard.
    with pytest.raises(ValueError) as error:
        validate_duration_budget(shots(*[7.5] * 10), 30)
    message = str(error.value)
    assert "75" in message and "30" in message
    assert "10 个镜头" in message
    # Fitting the budget passes, and so does rounding to the nearest legal clip.
    validate_duration_budget(shots(*[3] * 10), 30)
    validate_duration_budget(shots(*[3.4] * 10), 30)
    # Seven 5-second clips land on 35s: ordinary rounding, not a real overshoot.
    validate_duration_budget(shots(*[5] * 7), 30)


def test_budget_slack_is_a_fraction_but_never_less_than_one_clip():
    from app.services.task_worker import validate_duration_budget

    def shots(*durations):
        from types import SimpleNamespace
        return [SimpleNamespace(duration_seconds=value) for value in durations]

    # Large budget: slack is the larger 20% share.
    validate_duration_budget(shots(*[6] * 20), 100)        # 120s, inside +20%
    with pytest.raises(ValueError):
        validate_duration_budget(shots(*[6] * 21), 100)     # 126s, outside
    # Small budget: the one-clip floor keeps rounding from being rejected, while
    # a multiple-times overshoot is still refused.
    validate_duration_budget(shots(5, 5, 5, 5), 15)         # 20s, inside the floor
    with pytest.raises(ValueError):
        validate_duration_budget(shots(10, 10, 10), 15)     # 30s, double the budget


def test_budget_does_not_constrain_chapters_without_one():
    from app.services.task_worker import validate_duration_budget

    def shots(count, each):
        from types import SimpleNamespace
        return [SimpleNamespace(duration_seconds=each) for _ in range(count)]

    validate_duration_budget(shots(20, 10), None)   # 200s, no budget -> allowed


def test_budget_contract_is_omitted_when_there_is_no_budget():
    from app.services.task_worker import duration_budget_contract

    assert duration_budget_contract(None) == {}
    contract = duration_budget_contract(30)
    assert contract["chapter_target_duration_seconds"] == 30
    # The model must be told the sum, not just a per-shot cap.
    assert "总时长" in contract["budget_rule"]
    assert "30" in contract["budget_rule"]


def test_source_timeline_runtime_outranks_the_chapter_budget():
    # Explicit timestamps in the source state the runtime the author wants, so a
    # shorter AI-creation budget must not reject that board (or loop forever
    # asking the model to fit a length the text contradicts).
    from app.services.storyboard_generation import allocate_timeline

    source = "0-5秒 开场\n5-15秒 冲突\n15-60秒 高潮\n60-180秒 收尾"
    plan = allocate_timeline(source, [5, 10])
    assert plan, "explicit full timeline must produce a timing plan"
    assert sum(part["duration_seconds"] for part in plan) == pytest.approx(180)

    # A chapter without such a timeline yields no plan, which is the signal the
    # caller uses to decide whether the budget applies.
    assert allocate_timeline("纯文字章节，没有任何时间标记。", [5, 10]) == []


def test_a_media_task_without_an_agent_can_still_rewrite_prompts():
    # Media tasks carry no agent_profile_id and their model_id is an image model,
    # yet the safety-rewrite step needs a text agent. Both used to make that step
    # raise "任务所用 Agent 已停用", hiding the upstream policy rejection it was
    # meant to resolve; the tenant's general agent must be used instead.
    import asyncio

    from sqlalchemy import select

    from app.db.models import AgentKind, AgentProfile, Project
    from app.db.session import SessionLocal

    async def probe():
        async with SessionLocal() as session:
            project = await session.scalar(select(Project).limit(1))
            general = await session.scalar(
                select(AgentProfile).where(AgentProfile.kind == AgentKind.GENERAL).limit(1))
            return (project.text_model_id if project else None,
                    general.text_model_id if general else None, general is not None)

    project_text_model, agent_text_model, has_general = asyncio.run(probe())
    assert has_general, "a general agent is required for media prompt rewriting"
    assert agent_text_model or project_text_model, "the rewrite needs a text model to call"
