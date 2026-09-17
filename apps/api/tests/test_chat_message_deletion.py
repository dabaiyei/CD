import asyncio
from unittest.mock import AsyncMock

from app.db.models import AgentChatMessage, AgentChatSession, AgentMessageRole
from app.db.session import SessionLocal
from app.services.agent_runtime import AgentRuntimeClient


def records(client, headers):
    response = client.post('/api/v1/agent/sessions', headers=headers, json={'scene': 'workspace'})
    assert response.status_code == 201, response.text
    session_id = response.json()['id']

    async def seed():
        async with SessionLocal() as db:
            chat = await db.get(AgentChatSession, session_id)
            result = []
            for text in ['保留', '删除文字', '删除图片']:
                row = AgentChatMessage(tenant_id=chat.tenant_id, user_id=chat.user_id,
                    session_id=session_id, role=AgentMessageRole.ASSISTANT, content=text,
                    runtime_manifest={'generated_media': [{'id': 'image', 'media_url': '/uploads/example.webp'}]})
                db.add(row)
                await db.flush()
                result.append(row.id)
            await db.commit()
            return result
    return session_id, asyncio.run(seed())


def test_delete_selected_records_persists_and_resets_context(client, creator_headers, monkeypatch):
    reset = AsyncMock()
    monkeypatch.setattr(AgentRuntimeClient, 'delete_session', reset)
    session_id, ids = records(client, creator_headers)
    result = client.post(f'/api/v1/agent/sessions/{session_id}/messages/delete',
        headers=creator_headers, json={'message_ids': ids[1:]})
    assert result.status_code == 204, result.text
    detail = client.get(f'/api/v1/agent/sessions/{session_id}', headers=creator_headers).json()
    assert [row['id'] for row in detail['messages']] == ids[:1]
    reset.assert_awaited_once()


def test_delete_cannot_cross_users_or_sessions(client, creator_headers, admin_headers, monkeypatch):
    reset = AsyncMock()
    monkeypatch.setattr(AgentRuntimeClient, 'delete_session', reset)
    first, ids = records(client, creator_headers)
    second, other = records(client, creator_headers)
    path = f'/api/v1/agent/sessions/{first}/messages/delete'
    assert client.post(path, headers=admin_headers, json={'message_ids': ids}).status_code == 404
    assert client.post(path, headers=creator_headers, json={'message_ids': [ids[0], other[0]]}).status_code == 404
    assert len(client.get(f'/api/v1/agent/sessions/{first}', headers=creator_headers).json()['messages']) == 3
    reset.assert_not_awaited()


def test_runtime_failure_keeps_records(client, creator_headers, monkeypatch):
    monkeypatch.setattr(AgentRuntimeClient, 'delete_session', AsyncMock(side_effect=RuntimeError('offline')))
    session_id, ids = records(client, creator_headers)
    response = client.post(f'/api/v1/agent/sessions/{session_id}/messages/delete',
        headers=creator_headers, json={'message_ids': ids})
    assert response.status_code == 503
    assert len(client.get(f'/api/v1/agent/sessions/{session_id}', headers=creator_headers).json()['messages']) == 3
