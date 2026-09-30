import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from test_api import FakePersonalChatMediaActionRuntime

from app.db.models import AgentChatMessage, AgentChatSummary
from app.db.session import SessionLocal
from app.services.task_worker import process_task


@pytest.mark.parametrize("scope", ["personal", "project"])
def test_window_history_preserves_drafts_after_summary_and_isolates_other_windows(
    client,
    creator_headers,
    admin_headers,
    monkeypatch,
    scope,
):
    provider_id = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(
        f"/api/v1/admin/providers/{provider_id}", headers=admin_headers, json={"api_key": "test-context-key"}
    )
    path = "/api/v1/agent/sessions"
    if scope == "project":
        project_id = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
        path = f"/api/v1/projects/{project_id}/agent/sessions"
    sid = client.post(path, headers=creator_headers, json={"scene": "workspace"}).json()["id"]
    other = client.post(path, headers=creator_headers, json={"scene": "workspace"}).json()["id"]
    client.post(f"{path}/{other}/messages", headers=creator_headers, json={"content": "OTHER_WINDOW_SECRET"})
    image = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, format="PNG")
    attachment = client.post(
        path.removesuffix("sessions") + "attachments",
        headers=creator_headers,
        files={"file": ("reference.png", image.getvalue(), "image/png")},
    ).json()
    first = client.post(
        f"{path}/{sid}/messages",
        headers=creator_headers,
        json={"content": "请写一篇故事，主人公叫林月，不要改名。", "attachment_ids": [attachment["id"]]},
    )
    draft = "草稿开头。" * 650 + "MIDDLE_DETAIL_MUST_REMAIN" + "故事后半段。" * 650
    runtime = FakePersonalChatMediaActionRuntime(response=draft)
    asyncio.run(process_task(first.json()["task"]["id"], runtime_factory=lambda: runtime))

    async def summarize():
        async with SessionLocal() as db:
            last = await db.scalar(
                select(AgentChatMessage).where(
                    AgentChatMessage.session_id == sid,
                    AgentChatMessage.run_id == first.json()["task"]["id"],
                    AgentChatMessage.role == "ASSISTANT",
                )
            )
            assert last is not None
            db.add(
                AgentChatSummary(
                    tenant_id=last.tenant_id,
                    user_id=last.user_id,
                    session_id=sid,
                    version=1,
                    content="故事主人公林月。",
                    through_message_id=last.id,
                )
            )
            await db.commit()

    asyncio.run(summarize())
    second = client.post(
        f"{path}/{sid}/messages",
        headers=creator_headers,
        json={"content": "接着刚才的故事写，主人公叫什么？"},
    )
    followup = FakePersonalChatMediaActionRuntime(response="主人公叫林月。")
    asyncio.run(process_task(second.json()["task"]["id"], runtime_factory=lambda: followup))
    request = followup.requests[0]
    # Validate against the actual receiving service, not just the API's looser DTO.
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[3] / "services/agent-runtime/agentscope")
    )
    from runtime.contracts import AgentRunRequest

    AgentRunRequest.model_validate(request.model_dump(mode="json"))
    assert request.session_id == sid
    assert [item.id for item in request.attachments] == [attachment["id"]]
    assert request.conversation_summary == "故事主人公林月。"
    assert any("不要改名" in item["content"] for item in request.recent_messages)
    assert any("草稿开头" in item["content"] for item in request.recent_messages)
    archive = "\n".join(f.content for f in request.project_files if "conversation-history" in f.path)
    assert draft in archive
    assert "OTHER_WINDOW_SECRET" not in archive
    assert all(not f.editable for f in request.project_files if "conversation-history" in f.path)
    detail = client.get(f"/api/v1/tasks/{second.json()['task']['id']}", headers=creator_headers).json()
    assert detail["status"] == "succeeded"
    if scope == "project":
        from types import SimpleNamespace
        from app.services.chat_context import routing_evidence
        evidence = routing_evidence(request, SimpleNamespace(project_id=project_id, request_payload={}))
        assert evidence["project_id"] == project_id
        assert evidence["summary"] == "故事主人公林月。"
        assert any("不要改名" in item["content"] for item in evidence["history"])
        assert "OTHER_WINDOW_SECRET" not in str(evidence)
        assert "test-context-key" not in str(evidence)
