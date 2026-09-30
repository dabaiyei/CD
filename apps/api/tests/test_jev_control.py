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
    with pytest.raises(module.JevTransientPending, match="连接暂时失败"):
        asyncio.run(module.chat_intent(module.Controller(config()), {"message": "讨论"}))


def test_provider_503_is_resumable_and_does_not_discard_checkpoint(monkeypatch):
    async def post(*args):
        request = httpx.Request("POST", "https://opencode.ai/zen/v1/systemone")
        raise httpx.HTTPStatusError("temporarily unavailable", request=request,
                                    response=httpx.Response(503, request=request))

    monkeypatch.setattr(module, "post", post)
    with pytest.raises(module.JevTransientPending, match="HTTP 503"):
        asyncio.run(module.Controller(config()).choose(
            "单镜局部审核", {"shot_index": 44},
            {"frame_layout": module.choice("是否修改", {"edit": "修改", "keep": "保留"})},
        ))


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_jev_auth_and_contract_errors_require_configuration_fix(monkeypatch, status):
    async def post(*args):
        request = httpx.Request("POST", "https://opencode.ai/zen/v1/systemone")
        raise httpx.HTTPStatusError("configuration error", request=request,
                                    response=httpx.Response(status, request=request))

    monkeypatch.setattr(module, "post", post)
    with pytest.raises(module.JevDecisionPending) as error:
        asyncio.run(module.Controller(config()).choose(
            "单镜局部审核", {"shot_index": 44},
            {"frame_layout": module.choice("是否修改", {"edit": "修改", "keep": "保留"})},
        ))
    assert not isinstance(error.value, module.JevTransientPending)


def test_rate_limit_is_distinguished_from_configuration_failure(monkeypatch):
    async def post(*args):
        request = httpx.Request("POST", "https://opencode.ai/zen/v1/systemone")
        raise httpx.HTTPStatusError("rate limited", request=request,
                                    response=httpx.Response(429, request=request))

    monkeypatch.setattr(module, "post", post)
    with pytest.raises(module.JevRateLimitPending, match="限流"):
        asyncio.run(module.Controller(config()).choose(
            "单镜修复范围", {"shot": 1},
            {"frame_layout": module.choice("是否修改", {"edit": "修改", "keep": "保留"})},
        ))


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


def test_local_uncertainty_rechecks_only_unresolved_pair_and_reuses_decisions(monkeypatch):
    calls = []
    async def post(config, payload):
        calls.append(payload)
        if len(payload['questions']) > 1:
            return answers(identity_start='conflict', first_frame='unknown', layout='clear')
        assert list(payload['questions']) == ['first_frame']
        assert 'temporal_context' in payload['state']['evidence']
        return answers(first_frame='clear')

    monkeypatch.setattr(module, 'post', post)
    control = module.Controller(config(), {})
    shot = {'order_index': 1, 'scene_description': '人物先坐后站',
            'action_description': '0秒坐着，3秒起身', 'image_prompt': '人物坐着',
            'frame_layout': {'position': '椅子前'}}
    first = asyncio.run(module.local_review(control, shot))
    assert len(first) == 1 and first[0]['fields'] == ['scene_description', 'action_description']
    assert asyncio.run(module.local_review(control, shot)) == first
    assert len(calls) == 2


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


@pytest.mark.parametrize('value,confidence', [('unknown', .99), ('edit', .6)])
def test_repair_scope_defers_only_uncertain_linked_fields(monkeypatch, value, confidence):
    async def post(*args):
        reply = answers(frame_layout=value, image_prompt='edit', emotion_plan='keep')
        reply['answers']['frame_layout']['confidence'] = confidence
        return reply
    monkeypatch.setattr(module, 'post', post)
    deferred = []
    fields = asyncio.run(module.repair_fields(module.Controller(config()), {'order_index': 2},
        [{'fields': ['scene_description'], 'issue': '站位矛盾'}],
        ['scene_description', 'frame_layout', 'image_prompt', 'emotion_plan'],
        ['scene_description'], deferred=deferred))
    assert fields == ['frame_layout', 'image_prompt', 'scene_description']
    assert deferred == ['frame_layout']


def test_repair_scope_transport_failure_is_not_deferred(monkeypatch):
    async def post(*args):
        raise httpx.ReadTimeout('offline')
    monkeypatch.setattr(module, 'post', post)
    deferred = []
    with pytest.raises(module.JevDecisionPending):
        asyncio.run(module.repair_fields(module.Controller(config()), {'order_index': 2}, [],
            ['scene_description', 'frame_layout'], ['scene_description'], deferred=deferred))
    assert deferred == []


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


@pytest.mark.parametrize('approved', [True, False])
def test_unknown_local_review_hands_only_pending_pair_to_existing_reviewer(monkeypatch, approved):
    from test_storyboard_review import noop, request, shots

    from app.services.storyboard_review import review_board

    async def controller(tenant, checkpoint):
        return module.Controller(config(), checkpoint)

    monkeypatch.setattr(module, "controller", controller)
    mock_answers(monkeypatch, identity_start="clear", first_frame="unknown", layout="clear")

    calls = []
    class Runtime:
        async def run(self, req):
            calls.append(req)
            assert module.LOCAL_SCOPE not in req.system_prompt
            assert 'JEV局部审核交接' in req.system_prompt
            assert '"check": "first_frame"' in req.system_prompt
            assert '"check": "identity_start"' not in req.system_prompt
            findings = [] if approved else [{'shot_indices': [1], 'fields': ['image_prompt'],
                'severity': 'major', 'issue': '首帧与场景起点矛盾', 'suggestion': '只修改首帧起点'}]
            return SimpleNamespace(final_response=json.dumps({'approved': approved,
                'summary': '已核验首帧及其它原审核范围', 'findings': findings}), manifest={})

    state = {}
    async def run():
        result, _ = await review_board(request(), Runtime, shots=shots(1), script='', state=state,
            save=noop, progress=noop)
        assert result.approved is approved
        await review_board(request(), Runtime, shots=shots(1), script='', state=state,
            save=noop, progress=noop)
    asyncio.run(run())
    assert len(calls) == 1
    assert state['completed'] and state['jev_deferred_checks']


@pytest.mark.parametrize(
    "operation,authoring", [
        ("discuss", "no"), ("discuss", "yes"), ("discuss", "unknown"), ("revise", "no"), ("execute", "no"), ("unknown", "no")
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
        assert runtime.requests[0].tool_mode == "retrieval"
        assert "平台未确认本轮有执行操作的授权" in runtime.requests[0].system_prompt
    elif operation == "discuss":
        assert runtime.requests[0].tool_mode == "retrieval"
        if authoring == "yes":
            assert "JEV已确定创作模块" in runtime.requests[0].system_prompt
            assert "ACT视角" in runtime.requests[0].system_prompt
    else:
        assert runtime.requests[0].tool_mode == "workspace"
        assert '【镜头语言】' in runtime.requests[0].system_prompt


@pytest.mark.parametrize('decision_type,error_message', [
    ('jev_control', str(module.JevDecisionPending('证据不足'))),
    ('storyboard_input_budget', '[storyboard_input_budget] 本批输入超出预算'),
])
def test_pending_decision_parks_workflow_and_only_explicit_retry_resumes(tmp_path, monkeypatch, decision_type, error_message):
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
                    error_message=error_message,
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
            assert decision.decision_type == decision_type
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
def test_uncertain_shortcut_degrades_only_to_readonly(monkeypatch, operation):
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
        assert asyncio.run(module.project_entry("t", "含糊的任务", [])) == {
            "operation": "discuss", "action": "assistant", "clarification_needed": True}
