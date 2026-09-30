import asyncio
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr
from test_personal_routing import response, state

from app.core.config import get_settings
from app.services import personal_routing as routing
from app.services.media_gateway import OpenAICompatibleMediaGateway, _ImageRequestVariant
from app.services.personal_media_plan import latest_plan, execution_confirmation, save_proposed_plan


@pytest.mark.parametrize("text", ["你倒是开始任务啊", "继续任务", "请开始", "赶紧执行下一步", "开始"])
def test_explicit_confirmation_executes_saved_plan_without_classifier(text):
    ref = "11111111-1111-1111-1111-111111111111"
    value = state(text)
    value["attachments"] = [{"id": ref}]
    value["media_plan"] = latest_plan([SimpleNamespace(
        role="assistant", content=proposal(ref), id="new-plan", runtime_manifest={}
    )], value["attachments"], [])
    result = routing.merge_decision(value, {"answers": {}})
    assert result["output"] == "image"
    assert result["media_plan"]["source_message_id"] == "new-plan"
    assert result["creation"]["reference_attachment_ids"] == [ref]


@pytest.mark.parametrize("text", ["不要开始任务", "为什么没开始", "等", "先改提示词再开始", "开始是什么意思"])
def test_discussion_or_negation_is_not_execution(text):
    assert not execution_confirmation(text)


@pytest.mark.parametrize("current,history,expected", [
    ("全部开始", [], True),
    ("废话少说 开始", [{"role": "user", "content": "全部开始"}], True),
    ("开始", [{"role": "user", "content": "图片完成后自动生成视频"}], True),
    ("开始", [{"role": "assistant", "content": "全部开始，自动续接"}], False),
    ("开始", [{"role": "user", "content": "全部开始"}, {"role": "user", "content": "先出图给我看"}], False),
    ("全部开始", [{"role": "user", "content": "先出图给我看"}], True),
])
def test_chain_consent_survives_followup_without_classifier(current, history, expected):
    ref = "11111111-1111-1111-1111-111111111111"
    value = state(current)
    value["history"] = history
    value["attachments"] = [{"id": ref}]
    value["media_plan"] = latest_plan([SimpleNamespace(
        role="assistant", content=proposal(ref), id="draft", runtime_manifest={}
    )], value["attachments"], [])
    result = routing.merge_decision(value, {"answers": {}})
    assert result["output"] == "image"
    assert result["media_plan"]["auto_continue"] is expected


def test_new_proposal_saved_before_next_user_turn():
    ref = "11111111-1111-1111-1111-111111111111"
    class DB:
        async def execute(self, query):
            return SimpleNamespace(all=lambda: [])
        async def scalars(self, query):
            return SimpleNamespace(all=lambda: [])
    chat = SimpleNamespace(id="chat", tenant_id="tenant", user_id="user",
        runtime_manifest={"media_plan": {"status": "completed", "source_message_id": "old"}})
    message = SimpleNamespace(role="assistant", content=proposal(ref), id="new", runtime_manifest={})
    plan = asyncio.run(save_proposed_plan(DB(), chat, message))
    assert chat.runtime_manifest["media_plan"] == plan
    assert plan["source_message_id"] == "new" and plan["status"] == "proposed"
    assert len(plan["steps"]) == 2
    assert not plan["auto_continue"]


def proposal(ref):
    return (
        f"## 第 1 步 · 合成图片\n参考图 `{ref}`，9:16\n"
        "> 保持参考图人物五官和服装，把人物放到暖黄灯光的酒吧舞台中央，全身入镜。\n"
        "## 第 2 步 · 生成视频\n以上一步图片为首帧，时长12秒，9:16，720P\n"
        "> 保持上一步图片人物身份与服装，她在暖黄酒吧舞台上缓慢起舞，一镜到底。\n"
    )


def test_text_to_image_absent_reference_is_not_missing_upload():
    content = proposal("").replace("参考图 ``", "类型：text_to_image；实际参考图 ID：无（只能文生图）")
    plan = latest_plan([SimpleNamespace(role="assistant", content=content, id="draft", runtime_manifest={})], [], [])
    value = state("好了开始吧")
    value["media_plan"] = plan
    value["history"] = [{"role": "user", "content": "你先生成资产图然后生成视频吧"}]
    value["attachments"] = []
    result = routing.merge_decision(value, {"answers": {}})
    assert result["output"] == "image"
    assert result["creation"]["reference_attachment_ids"] == []
    assert result["media_plan"]["auto_continue"]


@pytest.mark.parametrize("instruction", ["全部开始", "你先生成资产图然后生成视频吧"])
def test_confirmed_new_plan_queues_first_step_and_chains(client, creator_headers, admin_headers, monkeypatch, instruction):
    from test_api import FakeAssetImageGateway, FakePersonalChatMediaActionRuntime, FakeVideoGateway
    from app.services.task_worker import process_task

    monkeypatch.setattr(get_settings(), "typesafe_api_key", SecretStr("test"))
    async def evaluate(value, **kwargs):
        return response(output="clarify")
    monkeypatch.setattr(routing, "evaluate", evaluate)
    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(f"/api/v1/admin/providers/{provider}", headers=admin_headers, json={"api_key": "fake"})
    sid = client.post("/api/v1/agent/sessions", headers=creator_headers, json={"scene": "workspace"}).json()["id"]
    content = proposal("").replace("参考图 ``", "text_to_image；参考图：无")
    runtime = FakePersonalChatMediaActionRuntime(response=content)
    first = client.post(f"/api/v1/agent/sessions/{sid}/messages", headers=creator_headers,
                        json={"content": instruction}).json()["task"]["id"]
    asyncio.run(process_task(first, runtime_factory=lambda: runtime))
    parent = client.get(f"/api/v1/tasks/{first}", headers=creator_headers).json()
    assert parent["status"] == "succeeded", parent.get("error_message")
    image_id = parent["result_payload"]["media_plan_next_task_id"]
    image_task = client.get(f"/api/v1/tasks/{image_id}", headers=creator_headers).json()
    assert image_task["status"] == "queued"
    assert image_task["request_payload"]["jev_route"]["media_plan"]["index"] == 0
    assert image_task["request_payload"]["attachment_ids"] == []
    images, videos = FakeAssetImageGateway(), FakeVideoGateway()
    gateway = SimpleNamespace(generate_image=images.generate_image, submit_video=videos.submit_video, poll_video=videos.poll_video)
    runtime.response = '{"approved":true,"reason":"通过"}'
    asyncio.run(process_task(image_id, runtime_factory=lambda: runtime, gateway_factory=lambda _: gateway))
    image_task = client.get(f"/api/v1/tasks/{image_id}", headers=creator_headers).json()
    assert image_task["status"] == "succeeded", image_task.get("error_message")
    video_id = image_task["result_payload"]["media_plan_next_task_id"]
    video_task = client.get(f"/api/v1/tasks/{video_id}", headers=creator_headers).json()
    assert video_task["request_payload"]["attachment_ids"] == [image_task["result_payload"]["generated_media"][0]["id"]]
    asyncio.run(process_task(video_id, runtime_factory=lambda: runtime, gateway_factory=lambda _: gateway))
    video_task = client.get(f"/api/v1/tasks/{video_id}", headers=creator_headers).json()
    assert video_task["status"] == "succeeded", video_task.get("error_message")
    assert len(images.requests) == 1


@pytest.mark.parametrize("missing", [False, True])
def test_confirmation_routes_exact_next_prompt_even_when_output_is_text(missing):
    ref = "11111111-1111-1111-1111-111111111111"
    plan = latest_plan(
        [SimpleNamespace(role="assistant", content=proposal(ref), id="draft", runtime_manifest={})],
        [{"id": ref}],
        [],
    )
    value = state("开始")
    value["media_plan"] = plan
    value["attachments"] = [] if missing else [{"id": ref}]
    result = routing.merge_decision(value, response(output="text"))
    if missing:
        assert result["output"] == "clarify"
        return
    assert result["output"] == "image"
    assert result["prompt"].startswith("保持参考图人物")
    assert result["creation"]["reference_attachment_ids"] == [ref]
    assert not result["media_plan"]["auto_continue"]


def test_switching_media_kind_does_not_inherit_wrong_video_prompt():
    value = state("生成一个苹果图片")
    value["creation"]["type"] = "video"
    value["creation"]["prompt"] = "旧视频提示词"
    result = routing.merge_decision(value, response(output="image", continuation="continue"))
    assert "旧视频" not in result["prompt"]


def test_duplicate_model_names_prefer_configured_default_in_plan_and_chat():
    models = [
        {
            "id": "old",
            "name": "gpt-image-2.5-flare",
            "model_id": "gpt-image-2.5-flare",
            "type": "image",
            "is_default": False,
        },
        {
            "id": "default",
            "name": "gpt-image-2.5-flare",
            "model_id": "gpt-image-2.5-flare",
            "type": "image",
            "is_default": True,
        },
    ]
    content = proposal("11111111-1111-1111-1111-111111111111").replace(
        "合成图片", "合成图片 gpt-image-2.5-flare"
    )
    plan = latest_plan(
        [SimpleNamespace(role="assistant", id="draft", content=content, runtime_manifest={})], [], models
    )
    assert plan["steps"][0]["model_id"] == "default"
    value = state("使用gpt-image-2.5-flare生成苹果图片")
    value["models"] = models
    assert routing.merge_decision(value, response(output="image"))["creation"]["model_id"] == "default"


def test_tls_failure_retries_first_variant_without_changing_body(monkeypatch):
    gateway = OpenAICompatibleMediaGateway(
        base_url="https://example.test/v1", api_key="test", extra_headers={}
    )
    calls = []

    async def post(client, endpoint, headers, variant):
        calls.append((headers["Idempotency-Key"], variant.body))
        if len(calls) < 3:
            raise httpx.ConnectError("TLS handshake failed")
        return httpx.Response(200, json={"ok": True})

    async def sleep(_):
        pass

    monkeypatch.setattr(gateway, "_post_image_variant", post)
    monkeypatch.setattr("app.services.media_gateway.asyncio.sleep", sleep)
    result = asyncio.run(
        gateway._post_image_variants(
            None,
            "images/generations",
            {"Idempotency-Key": "same-key"},
            [_ImageRequestVariant({"prompt": "a"}), _ImageRequestVariant({"prompt": "b"})],
        )
    )
    assert result.status_code == 200
    assert calls == [("same-key", {"prompt": "a"})] * 3

    async def permanent(*args):
        calls.append(("failed", {}))
        raise httpx.ConnectError("TLS down")

    monkeypatch.setattr(gateway, "_post_image_variant", permanent)
    calls.clear()
    with pytest.raises(httpx.ConnectError):
        asyncio.run(
            gateway._post_image_variants(
                None, "images/generations", {}, [_ImageRequestVariant({}), _ImageRequestVariant({})]
            )
        )
    assert len(calls) == 3


@pytest.mark.parametrize("approved", [True, False])
def test_image_then_video_chain_saves_image_and_uses_it_as_reference(
    client,
    creator_headers,
    admin_headers,
    monkeypatch,
    approved,
):
    from test_api import FakeAssetImageGateway, FakePersonalChatMediaActionRuntime, FakeVideoGateway

    from app.services.task_worker import process_task
    from app.services import jev_control

    async def module_decision(config, payload):
        values = ({'authoring': 'yes'} if 'authoring' in payload['questions'] else
                  {'combat': 'none', 'emotion': 'yes', 'locomotion': 'no', 'speech': 'no'})
        return {'answers': {k: {'type': 'choice', 'choice': v, 'confidence': .99} for k, v in values.items()}}
    monkeypatch.setattr(jev_control, 'post', module_decision)

    monkeypatch.setattr(get_settings(), "typesafe_api_key", SecretStr("test"))
    calls = []

    async def evaluate(value, **kwargs):
        calls.append(value)
        result = response(output="text")
        if len(calls) > 1:
            result["answers"]["plan_action"] = {"type": "choice", "choice": "execute", "confidence": 0.99}
            result["answers"]["chain"] = {"type": "choice", "choice": "auto", "confidence": 0.99}
        return result

    monkeypatch.setattr(routing, "evaluate", evaluate)
    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(f"/api/v1/admin/providers/{provider}", headers=admin_headers, json={"api_key": "fake"})
    sid = client.post("/api/v1/agent/sessions", headers=creator_headers, json={"scene": "workspace"}).json()[
        "id"
    ]
    image = BytesIO()
    Image.new("RGB", (128, 128), "blue").save(image, format="PNG")
    ref = client.post(
        "/api/v1/agent/attachments",
        headers=creator_headers,
        files={"file": ("ref.png", image.getvalue(), "image/png")},
    ).json()["id"]
    draft_runtime = FakePersonalChatMediaActionRuntime(response=proposal(ref))
    first = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        headers=creator_headers,
        json={"content": "先帮我写合成图片和跳舞视频两步方案的提示词", "attachment_ids": [ref]},
    ).json()
    asyncio.run(process_task(first["task"]["id"], runtime_factory=lambda: draft_runtime))
    runtime = FakePersonalChatMediaActionRuntime(
        response='{"approved":' + str(approved).lower() + ',"reason":"画面核验"}'
    )
    images, videos = FakeAssetImageGateway(), FakeVideoGateway()
    gateway = SimpleNamespace(
        generate_image=images.generate_image, submit_video=videos.submit_video, poll_video=videos.poll_video
    )
    started = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        headers=creator_headers,
        json={"content": "开始，图片成功后自动接着生成视频"},
    ).json()
    tid = started["task"]["id"]
    asyncio.run(process_task(tid, runtime_factory=lambda: runtime, gateway_factory=lambda _: gateway))
    task = client.get(f"/api/v1/tasks/{tid}", headers=creator_headers).json()
    assert task["status"] == "succeeded", task.get("error_message")
    image_result = task["result_payload"]["generated_media"][0]
    next_id = task["result_payload"]["media_plan_next_task_id"]
    child = client.get(f"/api/v1/tasks/{next_id}", headers=creator_headers).json()
    assert child["request_payload"]["attachment_ids"] == [image_result["id"]]
    assert len(images.requests) == 1
    asyncio.run(process_task(next_id, runtime_factory=lambda: runtime, gateway_factory=lambda _: gateway))
    child = client.get(f"/api/v1/tasks/{next_id}", headers=creator_headers).json()
    assert child["status"] == ("succeeded" if approved else "failed"), child.get("error_message")
    assert len(images.requests) == 1
    assert client.get(f"/api/v1/tasks/{tid}", headers=creator_headers).json()["status"] == "succeeded"
    assert runtime.requests[0].attachments[0].id == image_result["id"]
    if not approved:
        runtime.response = '{"approved":true,"reason":"恢复时核验通过"}'
        resumed = client.post(
            f"/api/v1/agent/sessions/{sid}/messages", headers=creator_headers, json={"content": "继续"}
        ).json()
        resume_id = resumed["task"]["id"]
        asyncio.run(
            process_task(resume_id, runtime_factory=lambda: runtime, gateway_factory=lambda _: gateway)
        )
        recovered = client.get(f"/api/v1/tasks/{resume_id}", headers=creator_headers).json()
        assert recovered["status"] == "succeeded", recovered.get("error_message")
        assert recovered["request_payload"]["attachment_ids"] == [image_result["id"]]
        assert len(images.requests) == 1
