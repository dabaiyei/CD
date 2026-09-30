import asyncio

import pytest

from app.services.personal_creation_guidance import personal_creation_guidance


def test_combat_authoring_has_concrete_action_camera_and_effect_direction():
    rules = personal_creation_guidance("帮我写东方神龙与女神战斗的视频提示词，12秒，UE5")
    for term in (
        "ACT视角",
        "1—2秒",
        "接触点",
        "同步跟移",
        "Lumen",
        "Niagara",
        "不再概括",
        "模型",
        "原样提交",
        "镜头语言",
        "移 Truck/Slide",
    ):
        assert term in rules
    assert len(rules) < 4700
    assert "人物行走速度参考" not in rules


def test_walking_and_speech_use_scene_specific_guidance():
    rules = personal_creation_guidance("写视频提示词：人物小跑，急促地说：“快跟上我！”")
    assert "4级" in rules
    assert "人物行走速度参考" in rules
    assert "人声等级" in rules
    assert "ACT视角" not in rules
    assert "口型" in rules


def test_unrelated_chat_and_image_dont_load_video_director():
    history = [{"role": "user", "content": "写一个打斗视频"}]
    assert not personal_creation_guidance("今天星期几", recent_messages=history)
    assert not personal_creation_guidance("生成一张风景图片", mode="image")
    assert "ACT视角" in personal_creation_guidance("继续", recent_messages=history)


@pytest.mark.parametrize('authoring', ['yes', 'unknown'])
def test_home_text_prompt_runtime_receives_rules_without_rendering(
    client, creator_headers, admin_headers, monkeypatch, authoring
):
    from pydantic import SecretStr
    from test_api import FakePersonalChatMediaActionRuntime
    from test_personal_routing import response

    from app.core.config import get_settings
    from app.services import jev_control, personal_routing
    from app.services.jev_configuration import JevSettings
    from app.services.task_worker import process_task

    monkeypatch.setattr(get_settings(), "typesafe_api_key", SecretStr("test"))

    async def evaluate(*args, **kwargs):
        return response(output="text")

    monkeypatch.setattr(personal_routing, "evaluate", evaluate)

    async def configuration(*args):
        return JevSettings(True, "test")

    async def post(config, payload):
        values = (
            {"authoring": authoring}
            if "authoring" in payload["questions"]
            else {"combat": "exchange", "emotion": "yes", "locomotion": "yes", "speech": "no"}
        )
        return {
            "answers": {k: {"type": "choice", "choice": v, "confidence": 0.99} for k, v in values.items()}
        }

    monkeypatch.setattr(jev_control, "get_jev_configuration", configuration)
    monkeypatch.setattr(jev_control, "post", post)
    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(f"/api/v1/admin/providers/{provider}", headers=admin_headers, json={"api_key": "fake"})
    sid = client.post("/api/v1/agent/sessions", headers=creator_headers, json={"scene": "workspace"}).json()[
        "id"
    ]
    submitted = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        headers=creator_headers,
        json={"content": "只写12秒打斗视频提示词，人物奔跑追击，双方连续变招"},
    ).json()
    runtime = FakePersonalChatMediaActionRuntime(response="ACT视角，0—2秒双方挥剑交锋。")
    asyncio.run(process_task(submitted["task"]["id"], runtime_factory=lambda: runtime))
    task = client.get(f"/api/v1/tasks/{submitted['task']['id']}", headers=creator_headers).json()
    assert task["status"] == "succeeded", task.get("error_message")
    assert runtime.requests
    if authoring == 'yes':
        assert "ACT视角" in runtime.requests[0].system_prompt
        assert "人物行走速度参考" in runtime.requests[0].system_prompt
        assert "接触点" in runtime.requests[0].system_prompt
    else:
        assert not task['result_payload'].get('jev_pending')
        assert task['result_payload']['jev_control']['optional_creation_guidance']['status'] == 'skipped'
    assert not task["result_payload"].get("generated_media")
    assert not task["result_payload"].get("media_plan_next_task_id")


def test_selected_modules_override_keywords_without_dropping_base_constraints():
    selected = {"combat": "none", "emotion": "yes", "locomotion": "no", "speech": "no"}
    rules = personal_creation_guidance(
        "写视频：坐着谈论昨日奔跑和战斗，无实际对白", selected_modules=selected
    )
    assert "ACT视角" not in rules
    assert "人物行走速度参考" not in rules
    assert "原样提交" in rules and "参考媒体" in rules
    assert "JEV已确定创作模块" in rules


@pytest.mark.parametrize("authoring", ["no", "unknown", "yes"])
def test_home_activation_preserves_history_and_never_falls_back(monkeypatch, authoring):
    from app.services import jev_control
    from app.services.jev_configuration import JevSettings
    from app.services.personal_creation_guidance import controlled_guidance

    calls = []

    async def controller(tenant, checkpoint):
        return jev_control.Controller(JevSettings(True, "fake"), checkpoint)

    async def post(config, payload):
        calls.append(payload)
        values = (
            {"authoring": authoring}
            if len(calls) == 1
            else {"combat": "exchange", "emotion": "yes", "locomotion": "yes", "speech": "no"}
        )
        return {
            "answers": {k: {"type": "choice", "choice": v, "confidence": 0.99} for k, v in values.items()}
        }

    monkeypatch.setattr(jev_control, "controller", controller)
    monkeypatch.setattr(jev_control, "post", post)
    history = [{"role": "user", "content": "为两位剑客写追逐交锋视频提示词"}]
    if authoring == "unknown":
        with pytest.raises(jev_control.JevDecisionPending):
            asyncio.run(controlled_guidance("t", "继续", recent_messages=history))
    else:
        rules = asyncio.run(controlled_guidance("t", "继续", recent_messages=history))
        assert bool(rules) == (authoring == "yes")
        if authoring == "yes":
            assert "ACT视角" in rules
            assert history[0]["content"] in str(calls[1]["state"]["evidence"])
    assert len(calls) == (2 if authoring == "yes" else 1)


@pytest.mark.parametrize('message', ['我们这次对话不用管之前的先', '什么不足'])
@pytest.mark.parametrize('stage', ['authoring', 'modules', 'configuration'])
def test_optional_text_guidance_never_blocks_or_enables_modules(monkeypatch, message, stage):
    from app.services import jev_control
    from app.services.personal_creation_guidance import controlled_guidance

    class Control:
        async def choose(self, label, evidence, questions):
            if stage == 'modules' and 'authoring' in questions:
                return {'authoring': 'yes'}
            raise jev_control.JevDecisionPending('confidence insufficient')

    async def controller(*args):
        if stage == 'configuration':
            raise jev_control.JevDecisionPending('configuration unavailable')
        return Control()

    monkeypatch.setattr(jev_control, 'controller', controller)
    checkpoint = {}
    assert asyncio.run(controlled_guidance('t', message, optional=True, checkpoint=checkpoint)) == ''
    assert checkpoint['optional_creation_guidance']['status'] == 'skipped'
    with pytest.raises(jev_control.JevDecisionPending):
        asyncio.run(controlled_guidance('t', message, optional=False))


def test_home_pending_modules_do_not_call_creative_or_media_models(client, creator_headers, monkeypatch):
    from test_personal_routing import response

    from app.services import jev_control, personal_routing
    from app.services.jev_configuration import JevSettings
    from app.services.task_worker import process_task

    async def configuration(*args):
        return JevSettings(True, "test")

    async def evaluate(*args, **kwargs):
        assert kwargs["use_laya"] is False
        return response(output="video", rewrite="yes")

    async def post(config, payload):
        return {"answers": {"authoring": {"type": "choice", "choice": "unknown", "confidence": 0.99}}}

    monkeypatch.setattr(personal_routing, "get_jev_configuration", configuration)
    monkeypatch.setattr(personal_routing, "evaluate", evaluate)
    monkeypatch.setattr(jev_control, "get_jev_configuration", configuration)
    monkeypatch.setattr(jev_control, "post", post)
    sid = client.post("/api/v1/agent/sessions", headers=creator_headers, json={"scene": "workspace"}).json()[
        "id"
    ]
    submitted = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        headers=creator_headers,
        json={"content": "帮我优化上次的打斗提示词并生成视频"},
    ).json()

    class ForbiddenRuntime:
        async def run(self, request):
            raise AssertionError("creative model must not decide instead of JEV")

    def forbidden_gateway(*args):
        raise AssertionError("no paid media when modules are pending")

    asyncio.run(
        process_task(
            submitted["task"]["id"], runtime_factory=ForbiddenRuntime, gateway_factory=forbidden_gateway
        )
    )
    task = client.get(f"/api/v1/tasks/{submitted['task']['id']}", headers=creator_headers).json()
    assert task["status"] == "succeeded", task.get("error_message")
    assert task["result_payload"]["jev_pending"].startswith("JEV待确认")
    assert not task["result_payload"].get("generated_media")
