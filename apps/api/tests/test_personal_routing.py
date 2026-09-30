import asyncio
from copy import deepcopy

from pydantic import SecretStr

from app.core.config import get_settings
from app.services import personal_routing as routing


def response(output="image", continuation="continue", references="keep", rewrite="no"):
    return {
        "model": "test",
        "answers": {
            k: {"type": "choice", "choice": v, "confidence": 0.99}
            for k, v in dict(
                output=output, continuation=continuation, references=references, rewrite=rewrite
            ).items()
        },
    }


def state(message="衣服改成红色"):
    return {
        "message": message,
        "history": [],
        "current_attachment_ids": ["second"],
        "attachments": [
            {"id": "first", "ordinal": 1, "source": "uploaded"},
            {"id": "result", "ordinal": 1, "source": "generated"},
            {"id": "second", "ordinal": 2, "source": "uploaded"},
        ],
        "models": [{"id": "img", "name": "图片模型", "model_id": "img-v1", "type": "image"}],
        "creation": {
            "type": "image",
            "model_id": "img",
            "prompt": "蓝衣人物",
            "rewrite": False,
            "reference_attachment_ids": ["first"],
            "options": {"resolution": "2K", "aspect_ratio": "16:9"},
        },
    }


def test_continuation_preserves_reference_model_and_options():
    result = routing.merge_decision(state(), response())
    assert result["output"] == "image"
    assert result["creation"]["reference_attachment_ids"] == ["first"]
    assert result["creation"]["model_id"] == "img"
    assert result["creation"]["options"] == {"resolution": "2K", "aspect_ratio": "16:9"}
    assert result["prompt"] == "蓝衣人物\n本轮修改要求：衣服改成红色"
    assert not result["rewrite"]


def test_ordinal_uses_upload_order_not_generated_images():
    result = routing.merge_decision(state("参考第二张图生成4K图片，9:16"), response(references="select"))
    assert result["creation"]["reference_attachment_ids"] == ["second"]
    assert result["creation"]["options"] == {"resolution": "4K", "aspect_ratio": "9:16"}


def test_image_to_video_keeps_ratio_not_image_resolution_or_model():
    result = routing.merge_decision(state("让它动起来8秒"), response(output="video"))
    assert result["creation"]["model_id"] is None
    assert result["creation"]["options"] == {"aspect_ratio": "16:9", "duration_seconds": 8}
    assert result["creation"]["reference_attachment_ids"] == ["first"]


def test_reference_preference_survives_text_turn():
    original = state("以后都参考第一张图")
    saved = routing.merge_decision(original, response(output="text", continuation="new"))
    original.update(message="生成一个人物", creation=saved["creation"])
    result = routing.merge_decision(original, response(continuation="new"))
    assert result["creation"]["reference_attachment_ids"] == ["first"]


def test_clear_references_does_not_reset_other_parameters():
    result = routing.merge_decision(state("不要参考图，纯文生图"), response())
    assert result["creation"]["reference_attachment_ids"] == []
    assert result["creation"]["options"]["resolution"] == "2K"


def test_missing_reference_never_silently_substituted():
    result = routing.merge_decision(state("参考第五张图"), response())
    assert result["output"] == "clarify"


def test_low_confidence_paid_action_is_not_executed():
    raw = response()
    raw["answers"]["output"]["confidence"] = 0.4
    result = routing.merge_decision(state(), raw)
    assert result["output"] == "clarify"


def test_text_never_becomes_media_due_to_history():
    result = routing.merge_decision(state("写一个视频脚本"), response(output="text", continuation="new"))
    assert result["output"] == "text"


def test_selected_skill_authorizes_rewrite():
    original = state()
    original["selected_skills"] = True
    assert routing.merge_decision(original, response())["rewrite"]


def test_personal_jev_media_runs_through_worker(client, creator_headers, admin_headers, monkeypatch):
    from io import BytesIO

    from PIL import Image
    from test_api import FakeAssetImageGateway, FakePersonalChatMediaActionRuntime

    from app.services.task_worker import process_task

    monkeypatch.setattr(get_settings(), "typesafe_api_key", SecretStr("test-only"))
    observed = []

    async def evaluate(value, **kwargs):
        observed.append(deepcopy(value))
        return response(continuation="new" if len(observed) == 1 else "continue")

    monkeypatch.setattr(routing, "evaluate", evaluate)
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(f"/api/v1/admin/providers/{provider_id}", headers=admin_headers, json={"api_key": "fake"})
    sid = client.post("/api/v1/agent/sessions", headers=creator_headers, json={"scene": "workspace"}).json()[
        "id"
    ]
    gateway = FakeAssetImageGateway()
    runtime = FakePersonalChatMediaActionRuntime(response="should not be called")
    image = BytesIO()
    Image.new("RGB", (128, 128), "blue").save(image, format="PNG")
    upload = client.post(
        "/api/v1/agent/attachments",
        headers=creator_headers,
        files={"file": ("reference.png", image.getvalue(), "image/png")},
    )
    assert upload.status_code in (200, 201), upload.text
    attachment_id = upload.json()["id"]
    for index, prompt in enumerate(["参考第一张图生成一张蓝衣人物图片16:9，2K", "把衣服换成红色"]):
        sent = client.post(
            f"/api/v1/agent/sessions/{sid}/messages",
            headers=creator_headers,
            json={"content": prompt, "attachment_ids": [attachment_id] if index == 0 else []},
        )
        assert sent.status_code == 202, sent.text
        tid = sent.json()["task"]["id"]
        asyncio.run(process_task(tid, runtime_factory=lambda: runtime, gateway_factory=lambda _: gateway))
        task = client.get(f"/api/v1/tasks/{tid}", headers=creator_headers).json()
        assert task["status"] == "succeeded", task.get("error_message") or task
        assert task["request_payload"]["jev_route"]["output"] == "image"
    assert len(gateway.requests) == 2
    assert gateway.requests[-1].resolution == "2K"
    assert gateway.requests[-1].aspect_ratio == "16:9"
    assert "蓝衣人物" in gateway.requests[-1].prompt and "红色" in gateway.requests[-1].prompt
    assert observed[-1]["creation"]["model_id"]
    assert observed[-1]["creation"]["reference_attachment_ids"] == [attachment_id]
    assert gateway.requests[0].reference_image_url.startswith("data:image/")
    assert gateway.requests[-1].reference_image_url == gateway.requests[0].reference_image_url
    assert not runtime.requests


def test_cached_route_does_not_call_provider_on_media_retry():
    from types import SimpleNamespace

    saved = {"output": "image", "prompt": "frozen"}
    task = SimpleNamespace(request_payload={"scope": "personal", "mode": "image", "jev_route": saved})
    assert asyncio.run(routing.prepare_route(None, task)) == saved


def test_jev_unavailable_does_not_generate_media(client, creator_headers, monkeypatch):
    import httpx
    from test_api import FakePersonalChatMediaActionRuntime

    from app.services.task_worker import process_task

    monkeypatch.setattr(get_settings(), "typesafe_api_key", SecretStr("test-only"))

    async def fail(_state, **kwargs):
        raise httpx.ReadTimeout("test")

    monkeypatch.setattr(routing, "evaluate", fail)
    sid = client.post("/api/v1/agent/sessions", headers=creator_headers, json={"scene": "workspace"}).json()[
        "id"
    ]
    first = client.post(
        f"/api/v1/agent/sessions/{sid}/messages", headers=creator_headers,
        json={"content": "主角是一只叫小蓝的猫，记住这个设定。"},
    )
    first_runtime = FakePersonalChatMediaActionRuntime(response="记住了，主角小蓝是一只猫。")
    asyncio.run(process_task(first.json()["task"]["id"], runtime_factory=lambda: first_runtime))
    sent = client.post(
        f"/api/v1/agent/sessions/{sid}/messages",
        headers=creator_headers,
        json={"content": "生成一张猫的图片"},
    )
    tid = sent.json()["task"]["id"]

    def forbidden(_provider):
        raise AssertionError("must not generate on routing timeout")

    runtime = FakePersonalChatMediaActionRuntime(response="我理解你想生成猫的图片，目前无法确认生成请求，请稍后重试。")
    asyncio.run(process_task(tid, runtime_factory=lambda: runtime, gateway_factory=forbidden))
    task = client.get(f"/api/v1/tasks/{tid}", headers=creator_headers).json()
    assert task["status"] == "succeeded"
    assert task["request_payload"]["jev_route"]["reason"] == "router_unavailable"
    assert not task["result_payload"].get("media_intent")
    assert len(runtime.requests) == 1
    assert "本轮不能调用图片或视频生成" in runtime.requests[0].system_prompt
    assert any("小蓝" in item["content"] for item in runtime.requests[0].recent_messages)
