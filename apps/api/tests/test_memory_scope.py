import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.models import AgentMemory
from app.services.agent_memory import retrieve_project_memories, scoped_conversation_memory_key


def test_memory_retrieval_is_home_session_or_exact_project_scoped(tmp_path):
    async def run():
        engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "scope.db"}')
        async with engine.begin() as connection:
            await connection.run_sync(AgentMemory.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        user = SimpleNamespace(tenant_id='tenant', id='user')
        async with sessions() as session:
            for name, project, source, tenant, owner in [
                ('home-a', None, 'a', 'tenant', 'user'),
                ('home-b', None, 'b', 'tenant', 'user'),
                ('unattributed', None, None, 'tenant', 'user'),
                ('project-p-a', 'p', 'pa', 'tenant', 'user'),
                ('project-p-b', 'p', 'pb', 'tenant', 'user'),
                ('project-q', 'q', 'qa', 'tenant', 'user'),
                ('other-owner', 'p', 'pa', 'tenant', 'other'),
                ('other-tenant', None, 'a', 'other', 'user'),
            ]:
                session.add(AgentMemory(id=name, tenant_id=tenant, user_id=owner,
                    project_id=project, source_session_id=source, namespace='story',
                    memory_key=name, content='共享关键词', salience=0.5))
            await session.commit()

            async def retrieve(project, sid=None):
                rows = await retrieve_project_memories(session, user=user, project_id=project,
                    session_id=sid, query='共享关键词', limit=20)
                return {r.memory.id for r in rows}

            assert await retrieve(None, 'a') == {'home-a'}
            assert await retrieve(None, 'b') == {'home-b'}
            assert await retrieve(None) == set()
            assert await retrieve('p', 'pa') == {'project-p-a', 'project-p-b'}
            assert await retrieve('q') == {'project-q'}
        await engine.dispose()
    asyncio.run(run())


def test_home_memory_write_keys_never_collide_across_sessions():
    key = lambda project, sid, name='narrative-structure': scoped_conversation_memory_key(name, '内容', project, sid)
    assert key(None, 'a') != key(None, 'b')
    assert key(None, 'a') == key(None, 'a')
    assert key('p', 'a') == key('p', 'b') == 'narrative-structure'
    assert key(None, 'a', 'x' * 120 + 'a') != key(None, 'a', 'x' * 120 + 'b')
    assert len(key(None, 'a', 'x' * 200)) <= 160


def test_home_conversations_save_same_key_separately_and_retrieve_only_their_own(client, creator_headers, admin_headers):
    from sqlalchemy import select
    from app.db.session import SessionLocal
    from app.services.task_worker import process_task
    from test_api import FakeAgentRuntime

    provider = client.get('/api/v1/admin/providers', headers=admin_headers).json()[0]['id']
    client.patch(f'/api/v1/admin/providers/{provider}', headers=admin_headers, json={'api_key': 'test-memory-scope'})
    session_ids = [client.post('/api/v1/agent/sessions', headers=creator_headers,
        json={'scene': 'workspace'}).json()['id'] for _ in range(2)]
    for sid in session_ids:
        response = client.post(f'/api/v1/agent/sessions/{sid}/messages', headers=creator_headers,
            json={'content': '写一个关于主角困境的文字故事'})
        task_id = response.json()['task']['id']
        runtime = FakeAgentRuntime()
        asyncio.run(process_task(task_id, runtime_factory=lambda: runtime))
        task = client.get(f'/api/v1/tasks/{task_id}', headers=creator_headers).json()
        assert task['status'] == 'succeeded', task.get('error_message')
        assert not runtime.requests[0].memory_context
        asyncio.run(process_task(task['result_payload']['memory_maintenance_task_id'], runtime_factory=lambda: runtime))

    async def stored():
        async with SessionLocal() as db:
            return list((await db.scalars(select(AgentMemory).where(
                AgentMemory.source_session_id.in_(session_ids)))).all())
    memories = asyncio.run(stored())
    assert len(memories) == 2 and len({m.memory_key for m in memories}) == 2
    response = client.post(f'/api/v1/agent/sessions/{session_ids[0]}/messages', headers=creator_headers,
        json={'content': '继续刚才的文字故事'})
    runtime = FakeAgentRuntime()
    task_id = response.json()['task']['id']
    asyncio.run(process_task(task_id, runtime_factory=lambda: runtime))
    task = client.get(f'/api/v1/tasks/{task_id}', headers=creator_headers).json()
    assert task['result_payload']['retrieved_memory_ids'] == [m.id for m in memories if m.source_session_id == session_ids[0]]
    assert runtime.requests[0].conversation_summary
    assert runtime.requests[0].recent_messages
