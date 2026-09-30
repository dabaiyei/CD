import asyncio
from io import BytesIO

from PIL import Image
from sqlalchemy import select

from app.db.models import AgentChatSession, AgentChatSummary, AITask, TaskStatus
from app.db.session import SessionLocal
from app.services.task_worker import process_task


def test_edit_in_place_truncates_branch_and_retains_attachment(client, creator_headers, admin_headers, monkeypatch):
    from app.api.routes.agent_chat import AgentRuntimeClient
    from test_api import FakeAgentRuntime
    resets = []
    async def reset(self, **kwargs):
        resets.append(kwargs)
    monkeypatch.setattr(AgentRuntimeClient, 'delete_session', reset)
    provider = client.get('/api/v1/admin/providers', headers=admin_headers).json()[0]['id']
    client.patch(f'/api/v1/admin/providers/{provider}', headers=admin_headers, json={'api_key': 'fake'})
    sid = client.post('/api/v1/agent/sessions', headers=creator_headers, json={'scene': 'workspace'}).json()['id']
    image = BytesIO()
    Image.new('RGB', (128, 128), 'blue').save(image, format='PNG')
    attachment = client.post('/api/v1/agent/attachments', headers=creator_headers,
                             files={'file': ('a.png', image.getvalue(), 'image/png')}).json()['id']
    mids = []
    for i in range(3):
        sent = client.post(f'/api/v1/agent/sessions/{sid}/messages', headers=creator_headers,
                           json={'content': f'故事第{i}轮', 'attachment_ids': [attachment] if i == 1 else []})
        assert sent.status_code == 202, sent.text
        mids.append(sent.json()['user_message']['id'])
        asyncio.run(process_task(sent.json()['task']['id'], runtime_factory=FakeAgentRuntime))
    before = client.get(f'/api/v1/agent/sessions/{sid}', headers=creator_headers).json()['messages']
    assert len(before) == 6
    assistant_id = before[1]['id']
    denied = client.post(f'/api/v1/agent/sessions/{sid}/messages', headers=creator_headers,
                         json={'content': 'wrong', 'edit_message_id': assistant_id})
    assert denied.status_code == 422
    failed = client.post(f'/api/v1/agent/sessions/{sid}/messages', headers=creator_headers,
                         json={'content': 'should rollback', 'edit_message_id': mids[1], 'text_model_id': 'missing'})
    assert failed.status_code in (404, 409, 422), failed.text
    assert len(client.get(f'/api/v1/agent/sessions/{sid}', headers=creator_headers).json()['messages']) == 6
    edited = client.post(f'/api/v1/agent/sessions/{sid}/messages', headers=creator_headers,
                         json={'content': '重新写第二轮', 'edit_message_id': mids[1], 'attachment_ids': [attachment]})
    assert edited.status_code == 202, edited.text
    assert edited.json()['user_message']['id'] == mids[1]
    detail = client.get(f'/api/v1/agent/sessions/{sid}', headers=creator_headers).json()
    assert [m['id'] for m in detail['messages']] == [before[0]['id'], before[1]['id'], mids[1]]
    assert detail['messages'][-1]['content'] == '重新写第二轮'
    assert detail['messages'][-1]['runtime_manifest']['attachments'][0]['id'] == attachment
    async def verify():
        async with SessionLocal() as db:
            chat = await db.get(AgentChatSession, sid)
            assert 'media_creation' not in (chat.runtime_manifest or {})
            assert not (await db.scalars(select(AgentChatSummary).where(AgentChatSummary.session_id == sid))).all()
            tasks = (await db.scalars(select(AITask).where(AITask.task_type == 'agent_memory_maintenance',
                AITask.request_payload['agent_chat_session_id'].as_string() == sid))).all()
            assert tasks and all(t.status == TaskStatus.CANCELLED for t in tasks)
    asyncio.run(verify())
    blocked = client.post(f'/api/v1/agent/sessions/{sid}/messages', headers=creator_headers,
                          json={'content': 'busy', 'edit_message_id': mids[0]})
    assert blocked.status_code == 409
    assert resets
