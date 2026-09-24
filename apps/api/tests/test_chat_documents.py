import asyncio
import base64

import pytest

from app.services.task_worker import process_task
from test_api import FakeAgentRuntime


@pytest.mark.parametrize("personal", [True, False])
def test_document_attachment_reaches_runtime_as_document_not_image(client, creator_headers, admin_headers, personal):
    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    client.patch(f"/api/v1/admin/providers/{provider}", headers=admin_headers, json={"api_key": "test-doc-runtime"})
    if personal:
        root = "/api/v1/agent"
        session_id = client.post(root + "/sessions", headers=creator_headers, json={"scene": "workspace"}).json()["id"]
    else:
        project = client.get("/api/v1/projects", headers=creator_headers).json()[0]["id"]
        root = f"/api/v1/projects/{project}/agent"
        options = client.get(root + "/options", headers=creator_headers).json()
        session_id = client.post(root + "/sessions", headers=creator_headers,
            json={"agent_profile_id": options["agents"][0]["id"]}).json()["id"]
    data = "请审核这句原文：你退后，看我杀敌。".encode()
    uploaded = client.post(root + "/attachments", headers=creator_headers,
        files={"file": ("source.txt", data, "text/plain")})
    assert uploaded.status_code == 201, uploaded.text
    attachment = uploaded.json()
    assert attachment["mime_type"] == "text/plain"
    sent = client.post(root + f"/sessions/{session_id}/messages", headers=creator_headers,
        json={"content": "读取文档并核对台词", "attachment_ids": [attachment["id"]]})
    assert sent.status_code == 202, sent.text
    runtime = FakeAgentRuntime()
    asyncio.run(process_task(sent.json()["task"]["id"], runtime_factory=lambda: runtime))
    req = runtime.requests[0]
    assert req.attachments == []
    assert len(req.documents) == 1
    assert base64.b64decode(req.documents[0].data) == data
    assert "你退后，看我杀敌" not in req.prompt
    # Follow-up keeps the same conversation's document available, not another user's files.
    followup = client.post(root + f"/sessions/{session_id}/messages", headers=creator_headers,
        json={"content": "再看刚才文档里的台词"})
    assert followup.status_code == 202, followup.text
    second = FakeAgentRuntime()
    asyncio.run(process_task(followup.json()["task"]["id"], runtime_factory=lambda: second))
    assert second.requests[0].documents[0].id == attachment["id"]


def test_invalid_document_is_rejected_without_becoming_an_image(client, creator_headers):
    response = client.post("/api/v1/agent/attachments", headers=creator_headers,
        files={"file": ("bad.pdf", b"not a PDF", "application/pdf")})
    assert response.status_code == 422


def test_another_account_cannot_attach_the_document(client, creator_headers, admin_headers):
    uploaded = client.post("/api/v1/agent/attachments", headers=creator_headers,
        files={"file": ("private.txt", b"private user document", "text/plain")}).json()
    session_id = client.post("/api/v1/agent/sessions", headers=admin_headers,
        json={"scene": "workspace"}).json()["id"]
    response = client.post(f"/api/v1/agent/sessions/{session_id}/messages", headers=admin_headers,
        json={"content": "读取文件", "attachment_ids": [uploaded["id"]]})
    assert response.status_code == 422
