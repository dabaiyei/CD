from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.security import SecretBox, create_access_token
from app.db.models import (
    AgentKind,
    AgentProfile,
    AIModel,
    AITask,
    CreditAccount,
    ModelType,
    Project,
    ScriptVersion,
    User,
)
from app.db.session import SessionLocal
from app.services.agent_runtime import AgentRuntimeResponse
from app.services.task_worker import claim_task, process_task, recover_stale_tasks


class CreationRuntime:
    def __init__(self):
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        inputs = json.loads(
            request.prompt.rsplit("\n输入：", 1)[1].split("\n严格按此 JSON Schema 返回：", 1)[0]
        )
        if "现在不要生成正式剧本" in request.prompt:
            payload = {
                "proposals": [
                    {
                        "title": f"{inputs['preferences'].get('feedback') or '雾港谜案'}{i}",
                        "introduction": "失踪档案中的秘密",
                        "premise": "侦探追查失踪者",
                    }
                    for i in range(3)
                ]
            }
        elif "chapter_titles 中生成" in request.prompt:
            payload = {
                "outline": "发现线索，追查真相",
                "story_bible": "主角林遥，谨慎的侦探。",
                "chapter_titles": [
                    f"港口谜案第{i + 1}章" for i in range(inputs["preferences"]["chapter_count"])
                ],
            }
        else:
            payload = {
                "outline": "发现线索，追查真相",
                "story_bible": "主角林遥，谨慎的侦探。",
                "chapters": [
                    {"title": title, "description": "林遥追查线索，发现新的疑点"}
                    for title in inputs["titles"]
                ],
            }
        return AgentRuntimeResponse(
            session_id=request.session_id,
            final_response=json.dumps(payload),
            finish_reason="stop",
            events=[],
            manifest={},
        )


class InterruptedCreationRuntime(CreationRuntime):
    async def run(self, request):
        if '"start_chapter": 6' in request.prompt:
            raise RuntimeError("模拟章节批次连接中断")
        return await super().run(request)


@pytest.fixture
def creation_headers(client: TestClient, creator_headers: dict):
    async def create_user():
        async with SessionLocal() as session:
            original = await session.scalar(select(User).where(User.email == "creator@cineforge.local"))
            user = User(
                tenant_id=original.tenant_id,
                email="ai-creation-test@example.test",
                display_name="原创测试",
                password_hash=original.password_hash,
            )
            session.add(user)
            await session.flush()
            session.add(CreditAccount(user_id=user.id, tenant_id=user.tenant_id, balance=Decimal("1000")))
            await session.commit()
            return user.id, create_access_token(
                user_id=user.id, tenant_id=user.tenant_id, role=user.role.value
            )

    user_id, token = asyncio.run(create_user())
    yield {"Authorization": f"Bearer {token}"}

    async def cleanup():
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()

    asyncio.run(cleanup())


def test_creation_persists_choices_artifacts_and_sequential_unlock(
    client: TestClient,
    creation_headers: dict,
    admin_headers: dict,
    monkeypatch,
) -> None:
    creator_headers = creation_headers
    monkeypatch.setattr(SecretBox, "decrypt", lambda self, value: "mock-creation-token")
    options = client.get("/api/v1/projects/options", headers=creator_headers).json()
    config = {
        "name": "原创测试",
        "creation_mode": "ai",
        "cinematic": True,
        "aspect_ratio": "16:9",
        "text_model_id": options["text_models"][0]["id"],
        "image_model_id": options["image_models"][0]["id"],
        "video_model_id": options["video_models"][0]["id"],
        "visual_handbook_id": options["visual_handbooks"][0]["id"],
        "director_handbook_id": options["director_handbooks"][0]["id"],
    }
    assert (
        client.post(
            "/api/v1/projects", headers=creator_headers, json={**config, "text_model_id": None}
        ).status_code
        == 422
    )
    created = client.post("/api/v1/projects", headers=creator_headers, json=config)
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    base = f"/api/v1/projects/{project_id}"
    for mutation in ({"aspect_ratio": "9:16"}, {"cinematic": False}, {"creation_mode": "import"}):
        assert client.patch(base, headers=creator_headers, json=mutation).status_code == 422
    assert client.get(base + "/ai-creation", headers=admin_headers).status_code == 404
    preferences = {"genre": "悬疑", "chapter_count": 7, "chapter_duration_seconds": 120}
    proposed = client.post(
        base + "/ai-creation",
        headers=creator_headers,
        json={"action": "propose", "revision": 0, "preferences": preferences},
    )
    assert proposed.status_code == 200, proposed.text
    runtime = CreationRuntime()
    task_id = proposed.json()["state"]["task_id"]
    assert asyncio.run(process_task(task_id, runtime_factory=lambda: runtime))
    ready_choices = client.get(base + "/ai-creation", headers=creator_headers).json()
    assert ready_choices["task_status"] == "succeeded", ready_choices
    assert len(ready_choices["state"]["proposals"]) == 3
    revision_before_feedback = ready_choices["state"]["revision"]
    revised = client.post(
        base + "/ai-creation",
        headers=creator_headers,
        json={
            "action": "propose",
            "revision": revision_before_feedback,
            "preferences": {**preferences, "feedback": "增加奇幻元素"},
        },
    )
    assert revised.status_code == 200, revised.text
    assert asyncio.run(process_task(revised.json()["state"]["task_id"], runtime_factory=CreationRuntime))
    ready_choices = client.get(base + "/ai-creation", headers=creator_headers).json()
    assert ready_choices["state"]["proposals"][0]["title"].startswith("增加奇幻元素")
    assert (
        client.post(
            base + "/ai-creation",
            headers=creator_headers,
            json={"action": "choose", "revision": 0, "proposal_index": 0},
        ).status_code
        == 409
    )
    chosen = client.post(
        base + "/ai-creation",
        headers=creator_headers,
        json={
            "action": "choose",
            "revision": ready_choices["state"]["revision"],
            "proposal_index": 1,
        },
    )
    assert chosen.status_code == 200, chosen.text
    # A new worker instance reconstructs the choice entirely from persisted state.
    outline_task_id = chosen.json()["state"]["task_id"]
    assert asyncio.run(claim_task(outline_task_id)) == outline_task_id

    async def expire_worker():
        async with SessionLocal() as session:
            task = await session.get(AITask, outline_task_id)
            task.lease_expires_at = datetime.now(UTC) - timedelta(minutes=10)
            await session.commit()

    asyncio.run(expire_worker())
    assert asyncio.run(recover_stale_tasks()) >= 1
    resumed_runtime = CreationRuntime()
    assert asyncio.run(process_task(outline_task_id, runtime_factory=lambda: resumed_runtime))
    assert len(resumed_runtime.requests) == 1
    assert "chapter_titles 中生成" in resumed_runtime.requests[0].prompt
    final = client.get(base + "/ai-creation", headers=creator_headers).json()
    assert final["state"]["phase"] == "ready", final
    chapters = client.get(base + "/chapters", headers=creator_headers).json()
    assert all("尚未创作" in row["original_content"] for row in chapters)
    assert [row["locked"] for row in chapters] == [False] + [True] * 6
    assert (
        client.get(base + f"/chapters/{chapters[1]['id']}/scripts", headers=creator_headers).status_code
        == 409
    )
    files = client.get(base + "/files", headers=creator_headers).json()
    assert {"故事大纲.md", "创作记忆与设定.md", "章节列表.json", "AI创作对话记录.json"} <= {
        f["name"] for f in files
    }
    saved_files = {
        item["name"]: client.get(base + f"/files/{item['id']}", headers=creator_headers).json()["content"]
        for item in files
    }
    assert saved_files["故事大纲.md"] == "发现线索，追查真相"
    assert "主角林遥" in saved_files["创作记忆与设定.md"]
    assert len(json.loads(saved_files["章节列表.json"])) == 7
    assert "增加奇幻元素" in saved_files["AI创作对话记录.json"]
    final_project = client.get(base, headers=creator_headers).json()
    assert final_project["name"] == ready_choices["state"]["proposals"][1]["title"]
    assert "checkpoint" not in final_project["creation_state"]
    assert "电影制作强制规范" in runtime.requests[0].system_prompt
    assert "固定画幅 16:9" in runtime.requests[0].system_prompt
    assert runtime.requests[0].model_binding["model"] == options["text_models"][0]["model_id"]

    async def add_script():
        from app.db.models import Chapter, ProjectFile, ProjectFileKind
        from app.services.ai_creation import chapter_continuity_context

        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            script = ScriptVersion(
                tenant_id=project.tenant_id,
                user_id=project.owner_id,
                project_id=project.id,
                chapter_id=chapters[0]["id"],
                version=1,
                title="第一章剧本",
                content="正式剧本内容",
            )
            session.add(script)
            await session.flush()
            first = await session.get(Chapter, chapters[0]["id"])
            first.active_script_version_id = script.id
            session.add(
                ProjectFile(
                    tenant_id=project.tenant_id,
                    user_id=project.owner_id,
                    project_id=project.id,
                    name="连续性测试.md",
                    kind=ProjectFileKind.MEMORY,
                    content="已发生的关键事件" * 1000,
                    file_metadata={"script_version_id": script.id, "chapter_id": first.id},
                )
            )
            await session.commit()
            second = await session.get(Chapter, chapters[1]["id"])
            memory = await chapter_continuity_context(session, second)
            assert "已发生的关键事件" in memory
            assert len(memory) < 1600
            # Switching the effective version cannot leak the previous version's memory.
            revised = ScriptVersion(
                tenant_id=project.tenant_id,
                user_id=project.owner_id,
                project_id=project.id,
                chapter_id=first.id,
                version=2,
                title="新版",
                content="修订后的结尾",
            )
            session.add(revised)
            await session.flush()
            first.active_script_version_id = revised.id
            await session.flush()
            memory = await chapter_continuity_context(session, second)
            assert "修订后的结尾" in memory
            assert "已发生的关键事件" not in memory

    asyncio.run(add_script())
    unlocked = client.get(base + "/chapters", headers=creator_headers).json()
    assert [row["locked"] for row in unlocked] == [False, False] + [True] * 5
    assert client.delete(base, headers=creator_headers).status_code == 204


def test_creation_input_excludes_large_checkpoints_and_limits_feedback():
    from app.services.ai_creation_worker import creation_inputs

    inputs = creation_inputs(
        {
            "preferences": {"chapter_count": 100, "feedback": "x" * 6000},
            "selected": {"premise": "y" * 8000},
            "creation_checkpoint": {"chapters": ["huge previous chapter"] * 100},
            "previous_proposals": ["unused"],
            "feedback_history": ["z" * 6000] * 100,
        }
    )
    assert "creation_checkpoint" not in inputs
    assert "previous_proposals" not in inputs
    assert len(inputs["feedback_history"]) == 4
    assert len(json.dumps(inputs)) < 7000


def test_project_models_and_editable_bible_override_global_defaults(
    client: TestClient,
    creation_headers: dict,
    monkeypatch,
) -> None:
    from app.api.routes.agent_chat import resolve_text_model
    from app.api.routes.storyboards import storyboard_text_agent_and_model
    from app.services.ai_creation import project_creation_guidance, save_creation_file
    from app.services.asset_tasks import resolve_general_agent_text_model
    from app.services.director_orchestration import _agent_and_model
    from app.services.image_model_routing import resolve_image_model
    from app.services.task_submission import create_queued_task
    from app.services.task_worker import (
        project_image_model_for_storyboard,
        project_video_model_for_storyboard,
    )

    monkeypatch.setattr(SecretBox, "decrypt", lambda self, value: "mock-creation-token")
    options = client.get("/api/v1/projects/options", headers=creation_headers).json()

    async def check_bindings():
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(User.email == "ai-creation-test@example.test"))
            base_text = await session.get(AIModel, options["text_models"][0]["id"])
            text_model = AIModel(
                tenant_id=user.tenant_id,
                provider_id=base_text.provider_id,
                model_id="project-exclusive-text",
                name="项目文本模型",
                model_type=ModelType.TEXT,
            )
            image_model = AIModel(
                tenant_id=user.tenant_id,
                provider_id=base_text.provider_id,
                model_id="project-exclusive-image",
                name="项目图片模型",
                model_type=ModelType.IMAGE,
            )
            session.add_all([text_model, image_model])
            await session.flush()
            project = Project(
                tenant_id=user.tenant_id,
                owner_id=user.id,
                name="模型路由验证",
                creation_mode="ai",
                cinematic=True,
                text_model_id=text_model.id,
                image_model_id=image_model.id,
                video_model_id=options["video_models"][0]["id"],
            )
            session.add(project)
            await session.flush()
            agent = await session.scalar(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == user.tenant_id,
                    AgentProfile.kind == AgentKind.SCREENPLAY,
                )
            )
            agent.text_model_id = None
            assert (
                await project_image_model_for_storyboard(
                    session,
                    project=project,
                    tenant_id=user.tenant_id,
                )
            ).id == image_model.id
            video_model = await session.get(AIModel, project.video_model_id)
            video_model.enabled = False
            with pytest.raises(RuntimeError, match="视频模型不可用"):
                await project_video_model_for_storyboard(session, project=project, tenant_id=user.tenant_id)
            video_model.enabled = True
            assert (await resolve_text_model(session, agent, user.tenant_id, project_id=project.id))[
                0
            ].id == text_model.id
            assert (await _agent_and_model(session, user.tenant_id, project_id=project.id))[
                1
            ].id == text_model.id
            assert (await storyboard_text_agent_and_model(session, user=user, project_id=project.id))[
                1
            ].id == text_model.id
            assert (await resolve_general_agent_text_model(session, user.tenant_id, project=project))[
                1
            ].id == text_model.id
            assert (
                await resolve_image_model(
                    session,
                    tenant_id=user.tenant_id,
                    resolution="1K",
                    fallback_model_id=image_model.id,
                    prefer_fallback=True,
                )
            ).id == image_model.id
            task, _event = await create_queued_task(
                session,
                user=user,
                project_id=project.id,
                task_type="asset_prompt_generation",
                model_id=base_text.id,
                cost=Decimal("0"),
                request_payload={},
                message="测试模型绑定",
            )
            assert task.model_id == text_model.id
            from app.db.models import ProjectFileKind

            await save_creation_file(
                session, project, "创作记忆与设定.md", "新设定：主角害怕海水", ProjectFileKind.MEMORY
            )
            guidance = await project_creation_guidance(session, project)
            assert "主角害怕海水" in guidance
            assert "project-exclusive-text" in guidance
            assert "project-exclusive-image" in guidance
            assert all(
                term in guidance for term in ("动作", "机位", "光影", "材质", "特效", "台词", "声音", "剧情")
            )
            await session.rollback()

    asyncio.run(check_bindings())
