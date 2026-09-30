import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import jev_control as module
from app.services.jev_configuration import JevSettings


def config():
    return JevSettings(True, "test-secret", provider="opencode_zen", model="jev-1.13-free")


def answers(**values):
    return {
        "answers": {
            key: {"type": "choice", "choice": value, "confidence": 0.99} for key, value in values.items()
        }
    }


def mock_answers(monkeypatch, **values):
    async def post(*args, **kwargs):
        return answers(**values)

    monkeypatch.setattr(module, "post", post)


@pytest.mark.parametrize(
    "value,confidence",
    [("unknown", 1), ("create", 0.7), ("create", True), ("create", float("nan")), ("invented", 1)],
)
def test_uncertain_or_invalid_decisions_never_fall_through(monkeypatch, value, confidence):
    async def post(*args):
        return {"answers": {"operation": {"type": "choice", "choice": value, "confidence": confidence}}}

    monkeypatch.setattr(module, "post", post)
    with pytest.raises(module.JevDecisionPending):
        asyncio.run(module.chat_intent(module.Controller(config()), {"message": "继续"}))


def test_transport_failure_is_explicit_pending_not_model_fallback(monkeypatch):
    async def post(*args):
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(module, "post", post)
    with pytest.raises(module.JevDecisionPending, match="未转交其它模型"):
        asyncio.run(module.chat_intent(module.Controller(config()), {"message": "讨论"}))


@pytest.mark.parametrize("authorization", ["execute", "discuss"])
def test_overlapping_operation_labels_resolve_with_jev_authorization(monkeypatch, authorization):
    calls = []

    async def post(config, payload):
        calls.append(payload)
        if "operation" in payload["questions"]:
            return {"answers": {"operation": {
                "type": "choice", "choice": "continue", "confidence": 0.23,
            }}}
        if "response_mode" in payload["questions"]:
            return answers(response_mode="context")
        return answers(authorization=authorization)

    monkeypatch.setattr(module, "post", post)
    checkpoint = {}
    evidence = {"message": "只提交修复后的脚本复审，不提取资产、不生图"}
    if authorization == "discuss":
        evidence["message"] = "需要补充什么？"
    result = asyncio.run(module.chat_intent(module.Controller(config(), checkpoint), evidence))
    assert result == authorization
    assert len(calls) == 3
    assert calls[2]["state"]["evidence"] == evidence
    assert "未转交其它模型" not in str(result)
    assert list(checkpoint.values()) == [{"response_mode": "context"}, {"authorization": authorization}]


def test_clarification_uses_current_turn_without_old_execution_instructions(monkeypatch):
    calls = []

    async def post(config, payload):
        calls.append(payload)
        if "operation" in payload["questions"]:
            return answers(operation="unknown")
        assert payload["state"]["evidence"] == {"message": "需要补充什么"}
        return answers(response_mode="answer")

    monkeypatch.setattr(module, "post", post)
    evidence = {"message": "需要补充什么", "history": [{"role": "user", "content": "全部生图"}]}
    assert asyncio.run(module.chat_intent(module.Controller(config()), evidence)) == "discuss"
    assert len(calls) == 2


@pytest.mark.parametrize("value,confidence", [("unknown", 0.99), ("execute", 0.79), ("execute", True)])
def test_authorization_fallback_does_not_relax_threshold(monkeypatch, value, confidence):
    async def post(config, payload):
        if "operation" in payload["questions"]:
            return answers(operation="unknown")
        return {"answers": {"authorization": {
            "type": "choice", "choice": value, "confidence": confidence,
        }}}

    monkeypatch.setattr(module, "post", post)
    with pytest.raises(module.JevDecisionPending):
        asyncio.run(module.chat_intent(module.Controller(config()), {"message": "继续"}))


def test_checkpoints_reuse_exact_evidence_but_rejudge_changed_fields(monkeypatch):
    calls = []

    async def post(config, payload):
        calls.append(payload)
        return answers(operation="discuss")

    monkeypatch.setattr(module, "post", post)
    checkpoint = {}

    async def run():
        for message in ["看看上一章", "看看上一章", "看看下一章"]:
            await module.chat_intent(module.Controller(config(), checkpoint), {"message": message})

    asyncio.run(run())
    assert len(calls) == 2
    assert "test-secret" not in json.dumps(checkpoint)


def test_modules_use_independent_flags_instead_of_keyword_override(monkeypatch):
    mock_answers(monkeypatch, combat="none", emotion="yes", speech="yes", locomotion="no")
    selected = asyncio.run(
        module.modules(
            module.Controller(config()),
            {"action_description": "两人坐着讨论昨天的战斗，今天不再交手", "dialogue": "先休息。"},
        )
    )
    assert selected == {"combat": "none", "emotion": "yes", "speech": "yes", "locomotion": "no"}


def test_local_finding_targets_only_provided_shot_and_actual_fields(monkeypatch):
    mock_answers(monkeypatch, identity_start="conflict", first_frame="clear", layout="clear")
    shot = {
        "order_index": 17,
        "scene_description": "唯一女子全程穿白衣。",
        "action_description": "同一女子全程穿红衣，服装从未变化。",
    }
    result = asyncio.run(module.local_review(module.Controller(config()), shot))
    assert result[0]["shot_indices"] == [17]
    assert result[0]["fields"] == ["scene_description", "action_description"]
    assert "全程穿白衣" in result[0]["issue"] and "全程穿红衣" in result[0]["issue"]


def test_repair_scope_cannot_add_fields_or_drop_named_targets(monkeypatch):
    mock_answers(monkeypatch, image_prompt="edit", emotion_plan="keep", dialogue="edit")
    fields = asyncio.run(
        module.repair_fields(
            module.Controller(config()),
            {"order_index": 4, "scene_description": "白衣", "image_prompt": "红衣"},
            [{"issue": "服装矛盾"}],
            ["scene_description", "image_prompt", "emotion_plan"],
            ["scene_description"],
        )
    )
    assert fields == ["image_prompt", "scene_description"]


@pytest.mark.parametrize("conflict", [True, False])
def test_local_review_owns_its_scope_and_never_waives_broader_review(monkeypatch, conflict):
    from test_storyboard_review import noop, request, shots

    from app.services.storyboard_review import review_board

    calls = []

    async def controller(tenant, checkpoint):
        return module.Controller(config(), checkpoint)

    monkeypatch.setattr(module, "controller", controller)
    mock_answers(
        monkeypatch, identity_start="conflict" if conflict else "clear", first_frame="clear", layout="clear"
    )

    class Runtime:
        async def run(self, req):
            assert module.LOCAL_SCOPE in req.system_prompt
            calls.append(req)
            return SimpleNamespace(
                final_response=json.dumps(
                    {"approved": True, "summary": "原文与邻镜审核通过", "findings": []}
                ),
                manifest={},
            )

    rows = shots(1)
    rows[0].update(
        scene_description="女子全程白衣", action_description="女子全程红衣" if conflict else "女子全程白衣"
    )
    state = {}

    async def run():
        verdict, _ = await review_board(
            request(), Runtime, shots=rows, script="", state=state, save=noop, progress=noop
        )
        assert verdict.approved is not conflict
        await review_board(request(), Runtime, shots=rows, script="", state=state, save=noop, progress=noop)

    asyncio.run(run())
    assert len(calls) == (0 if conflict else 1)
    assert state["completed"] and state["jev_control"]


def test_unknown_local_review_does_not_call_another_model_or_mark_complete(monkeypatch):
    from test_storyboard_review import noop, request, shots

    from app.services.storyboard_review import review_board

    async def controller(tenant, checkpoint):
        return module.Controller(config(), checkpoint)

    monkeypatch.setattr(module, "controller", controller)
    mock_answers(monkeypatch, identity_start="unknown")

    def forbidden():
        raise AssertionError("must not call another model")

    state = {}
    with pytest.raises(module.JevDecisionPending):
        asyncio.run(
            review_board(
                request(), forbidden, shots=shots(1), script="", state=state, save=noop, progress=noop
            )
        )
    assert not state["completed"]


@pytest.mark.parametrize(
    "operation,authoring", [
        ("discuss", "no"), ("discuss", "yes"), ("revise", "no"), ("execute", "no"), ("unknown", "no")
    ]
)
def test_project_chat_enforces_authorization_at_tools_and_publication(
    client, creator_headers, admin_headers, monkeypatch, operation, authoring
):
    import hashlib

    from test_api import FakeAgentRuntime

    from app.services.task_worker import process_task

    async def controller(tenant, checkpoint):
        return module.Controller(config(), checkpoint)

    monkeypatch.setattr(module, "controller", controller)
    mock_answers(
        monkeypatch,
        operation="unknown" if operation == "execute" else operation,
        response_mode="context",
        authorization=operation,
        action="assistant",
        authoring=authoring,
        combat="exchange",
        emotion="yes",
        locomotion="no",
        speech="no",
    )
    project = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
    original = client.post(
        f"/api/v1/projects/{project}/files",
        headers=creator_headers,
        json={"name": f"jev-control-{operation}.md", "kind": "memory", "content": "original"},
    ).json()
    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(f"/api/v1/admin/providers/{provider}", headers=admin_headers, json={"api_key": "fake"})
    session = client.post(
        f"/api/v1/projects/{project}/agent/sessions", headers=creator_headers, json={}
    ).json()["id"]
    runtime = FakeAgentRuntime(
        [
            {
                "operation": "update",
                "file_id": original["id"],
                "content": "changed",
                "base_sha256": hashlib.sha256(b"original").hexdigest(),
            }
        ]
    )
    sent = client.post(
        f"/api/v1/projects/{project}/agent/sessions/{session}/messages",
        headers=creator_headers,
        json={
            "content": "只写两位剑客交锋视频提示词，不修改文件"
            if authoring == "yes"
            else "更新指定项目记忆"
            if operation in {"revise", "execute"}
            else "只讨论项目，不修改文件"
        },
    )
    assert sent.status_code == 202, sent.text
    asyncio.run(process_task(sent.json()["task"]["id"], runtime_factory=lambda: runtime))
    saved = client.get(f"/api/v1/projects/{project}/files/{original['id']}", headers=creator_headers).json()
    assert saved["content"] == ("changed" if operation in {"revise", "execute"} else "original")
    if operation == "unknown":
        assert not runtime.requests
    elif operation == "discuss":
        assert runtime.requests[0].tool_mode == "retrieval"
        if authoring == "yes":
            assert "JEV已确定创作模块" in runtime.requests[0].system_prompt
            assert "ACT视角" in runtime.requests[0].system_prompt
    else:
        assert runtime.requests[0].tool_mode == "workspace"
        assert '【镜头语言】' in runtime.requests[0].system_prompt


def test_pending_decision_parks_workflow_and_only_explicit_retry_resumes(tmp_path, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import session as db_session
    from app.db.models import (
        AITask,
        DirectorChildRun,
        DirectorDecisionRequest,
        DirectorWorkflowRun,
        Notification,
    )
    from app.services import director_orchestration as service

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pending.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db_session, "SessionLocal", sessions)
        async with engine.begin() as connection:
            for model in (
                AITask,
                DirectorWorkflowRun,
                DirectorChildRun,
                DirectorDecisionRequest,
                Notification,
            ):
                await connection.run_sync(model.__table__.create)
        owner = dict(tenant_id="t", user_id="u", project_id="p")
        async with sessions() as db:
            db.add(
                AITask(
                    id="task",
                    **owner,
                    task_type="director_storyboard_review",
                    status=service.TaskStatus.FAILED,
                    error_message=str(module.JevDecisionPending("证据不足")),
                    request_payload={"storyboard_review_cache": {"completed": {"saved": {}}}},
                )
            )
            db.add(
                DirectorWorkflowRun(
                    id="workflow",
                    **owner,
                    chapter_id="chapter",
                    stage=service.DirectorWorkflowStage.STORYBOARD_REVIEWING,
                    status=service.DirectorWorkflowStatus.RUNNING,
                    automation_mode=True,
                    current_task_id="task",
                )
            )
            db.add(
                DirectorChildRun(
                    id="child",
                    **owner,
                    workflow_id="workflow",
                    task_id="task",
                    kind="storyboard_review",
                    title="review",
                    status=service.DirectorChildStatus.RUNNING,
                )
            )
            await db.commit()
        retries = []

        async def retry(db, workflow, child, task):
            retries.append(task.request_payload)
            workflow.status = service.DirectorWorkflowStatus.RUNNING
            return SimpleNamespace(result_payload={}), None

        async def dispatch(db, queued):
            await db.commit()

        monkeypatch.setattr(service, "_retry_child", retry)
        monkeypatch.setattr(service, "_commit_and_dispatch", dispatch)
        await service._advance_director_task_terminal("task")
        await service._advance_director_task_terminal("task")
        assert not retries
        async with sessions() as db:
            workflow = await db.get(DirectorWorkflowRun, "workflow")
            decision = await db.scalar(select(DirectorDecisionRequest))
            assert workflow.status == service.DirectorWorkflowStatus.WAITING_USER
            assert decision.decision_type == "jev_control"
            with pytest.raises(ValueError):
                await service.submit_decision(db, workflow, decision, option="continue_anyway", feedback="")
            await service.submit_decision(db, workflow, decision, option="partial_repair", feedback="")
            assert decision.resolved
        assert len(retries) == 1
        assert retries[0]["storyboard_review_cache"]["completed"]
        await engine.dispose()

    asyncio.run(run())


def test_jev_entry_preserves_existing_video_task_dispatch(
    client, creator_headers, admin_headers, monkeypatch
):
    from test_api import test_director_agent_routes_natural_video_commands_to_platform_tasks

    async def controller(tenant, checkpoint=None):
        return module.Controller(config(), checkpoint)

    async def post(config, payload):
        questions = payload["questions"]
        if "operation" in questions:
            return answers(operation="create")
        if "action" in questions:
            message = payload["state"]["evidence"]["message"]
            return answers(action="video_prompt" if "提示词" in message else "video")
        return answers(combat="none", emotion="yes", locomotion="yes", speech="no")

    monkeypatch.setattr(module, "controller", controller)
    monkeypatch.setattr(module, "post", post)
    test_director_agent_routes_natural_video_commands_to_platform_tasks(
        client, creator_headers, admin_headers
    )


@pytest.mark.parametrize("operation", ["discuss", "create", "revise", "continue"])
def test_uncertain_shortcut_can_only_be_resolved_as_readonly(monkeypatch, operation):
    async def controller(tenant, checkpoint=None):
        return module.Controller(config(), checkpoint)

    async def post(config, payload):
        if "action" in payload["questions"]:
            return answers(action="unknown")
        return answers(operation=operation)

    monkeypatch.setattr(module, "controller", controller)
    monkeypatch.setattr(module, "post", post)
    if operation == "discuss":
        assert asyncio.run(module.project_entry("t", "只写文字", [])) == {
            "operation": "discuss",
            "action": "assistant",
        }
    else:
        with pytest.raises(module.JevDecisionPending):
            asyncio.run(module.project_entry("t", "含糊的任务", []))
