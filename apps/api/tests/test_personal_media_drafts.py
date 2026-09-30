import asyncio
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
from pydantic import SecretStr
from test_personal_routing import response, state

from app.core.config import get_settings
from app.services import personal_routing as routing
from app.services.personal_media_drafts import latest_prompt_draft

PROMPT = "同一位白衣女生在暖黄灯光下跳舞，保持五官与服装。动作连贯，镜头缓慢推进。时长12秒，9:16，720P。"
REPLY = f"## 主版本\n**中文提示词**\n\n> {PROMPT}\n\n**英文版**\n> English alternative\n\n## 备选\n另一个方案"


def messages():
    return [
        SimpleNamespace(
            id="user",
            role="user",
            content="写图片中女生跳舞的视频提示词，12秒",
            runtime_manifest={"attachments": [{"id": "first", "mime_type": "image/webp"}]},
        ),
        SimpleNamespace(id="draft", role="assistant", content=REPLY, runtime_manifest={}),
    ]


def test_extract_verbatim_primary_without_english_or_variants():
    draft = latest_prompt_draft(messages())
    assert draft["prompt"] == PROMPT
    assert draft["options"] == {"duration_seconds": 12, "aspect_ratio": "9:16", "resolution": "720P"}
    assert draft["reference_attachment_ids"] == ["first"]


def test_generate_from_draft_even_when_jev_calls_it_new():
    value = state("那你直接基于这个图片和提示词 帮我生成视频吧")
    value["prompt_draft"] = latest_prompt_draft(messages())
    result = routing.merge_decision(value, response(output="video", continuation="new", references="select"))
    assert result["output"] == "video"
    assert result["prompt"] == PROMPT
    assert result["creation"]["reference_attachment_ids"] == ["first"]
    assert result["creation"]["options"]["duration_seconds"] == 12
    assert result["creation"]["source_message_id"] == "draft"


def test_current_parameter_overrides_draft():
    value = state("按照这个提示词生成视频，改成16:9，10秒")
    value["prompt_draft"] = latest_prompt_draft(messages())
    result = routing.merge_decision(value, response(output="video"))
    assert result["creation"]["options"]["duration_seconds"] == 10
    assert result["creation"]["options"]["aspect_ratio"] == "16:9"


def test_missing_draft_does_not_submit_command_as_prompt():
    value = state("基于这个图片和提示词生成视频吧")
    assert routing.merge_decision(value, response(output="video"))["output"] == "clarify"


def test_unrelated_request_does_not_reuse_draft():
    value = state("生成海边日出的视频")
    value["prompt_draft"] = latest_prompt_draft(messages())
    result = routing.merge_decision(value, response(output="video", continuation="new"))
    assert result["prompt"] == value["message"]
    assert "source_message_id" not in result["creation"] or result["creation"]["source_message_id"] is None


def test_deleted_draft_reference_does_not_switch_to_text_video():
    value = state("基于这个图片和提示词生成视频")
    value["prompt_draft"] = latest_prompt_draft(messages())
    value["attachments"] = []
    assert routing.merge_decision(value, response(output="video"))["output"] == "clarify"


def test_two_turn_draft_to_video_sends_reference_and_prompt(
    client, creator_headers, admin_headers, monkeypatch
):
    from test_api import FakePersonalChatMediaActionRuntime, FakeVideoGateway

    from app.services.task_worker import process_task

    monkeypatch.setattr(get_settings(), "typesafe_api_key", SecretStr("test-only"))
    calls = []

    async def evaluate(value, **kwargs):
        calls.append(value)
        return response(output="text" if len(calls) == 1 else "video", continuation="new")

    monkeypatch.setattr(routing, "evaluate", evaluate)
    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(f"/api/v1/admin/providers/{provider}", headers=admin_headers, json={"api_key": "test-key"})
    sid = client.post("/api/v1/agent/sessions", headers=creator_headers, json={"scene": "workspace"}).json()[
        "id"
    ]
    image = BytesIO()
    Image.new("RGB", (128, 128), "blue").save(image, format="PNG")
    upload = client.post(
        "/api/v1/agent/attachments",
        headers=creator_headers,
        files={"file": ("test.png", image.getvalue(), "image/png")},
    ).json()
    runtime = FakePersonalChatMediaActionRuntime(response=REPLY)
    gateway = FakeVideoGateway()
    for index, content in enumerate(
        ["帮我写图片中女生跳舞的视频提示词，12秒", "那你直接基于这个图片和提示词 帮我生成视频吧"]
    ):
        sent = client.post(
            f"/api/v1/agent/sessions/{sid}/messages",
            headers=creator_headers,
            json={"content": content, "attachment_ids": [upload["id"]] if index == 0 else []},
        )
        assert sent.status_code == 202
        tid = sent.json()["task"]["id"]
        asyncio.run(process_task(tid, runtime_factory=lambda: runtime, gateway_factory=lambda _: gateway))
        task = client.get(f"/api/v1/tasks/{tid}", headers=creator_headers).json()
        assert task["status"] == "succeeded", task
    assert len(gateway.requests) == 1
    assert PROMPT in gateway.requests[0].prompt
    assert '那你直接基于' not in gateway.requests[0].prompt
    assert gateway.requests[0].aspect_ratio == "9:16"
    assert gateway.requests[0].reference_media[0]["data_uri"].startswith("data:image/")
    assert task["request_payload"]["media_options"]["duration_seconds"] == 12
    assert task["request_payload"]["attachment_ids"] == [upload["id"]]
