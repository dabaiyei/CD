import asyncio

import pytest

from app.services import jev_control


def test_ambiguous_shortcut_resolves_explicit_edit_without_lowering_confidence(monkeypatch):
    class Control:
        async def choose(self, label, evidence, questions):
            if 'action' in questions:
                raise jev_control.JevDecisionPending('ambiguous category')
            if 'operation' in questions:
                return {'operation': 'revise'}
            assert 'image_edit' in questions
            return {'image_edit': 'edit'}

    async def controller(*args):
        return Control()

    monkeypatch.setattr(jev_control, 'controller', controller)
    result = asyncio.run(jev_control.project_entry('t', '把冬哥和坤哥的五台山背景改成峨眉山，重新生成资产', []))
    assert result == {'operation': 'revise', 'action': 'asset_edit'}


@pytest.mark.usefixtures('jev_disabled')
def test_chat_edit_uses_current_derivative_image_and_keeps_instruction(client, creator_headers, admin_headers, monkeypatch):
    from test_api import test_agent_chat_queues_asset_prompt_and_image_tasks_from_platform_command, FakeAssetImageGateway
    from app.services.task_worker import process_task

    test_agent_chat_queues_asset_prompt_and_image_tasks_from_platform_command(client, creator_headers, admin_headers)
    project = client.get('/api/v1/projects', headers=creator_headers).json()[0]['id']
    asset_url = f'/api/v1/projects/{project}/assets'
    asset = next(a for a in client.get(asset_url, headers=creator_headers).json() if a['name'] == 'AgentAssetLinyao')
    parent = client.post(asset_url, headers=creator_headers, json={
        'name': 'EditParentWithoutImage', 'asset_type': 'character', 'description': '基础人物'}).json()
    patched = client.patch(f'/api/v1/assets/{asset["id"]}', headers=creator_headers, json={'parent_asset_id': parent['id']})
    assert patched.status_code == 200, patched.text
    original_url = patched.json()['media_url']

    async def entry(*args, **kwargs):
        return {'operation': 'revise', 'action': 'asset_edit'}

    monkeypatch.setattr(jev_control, 'project_entry', entry)
    session = client.post(f'/api/v1/projects/{project}/agent/sessions', headers=creator_headers,
        json={'scene': 'workspace'}).json()['id']
    message = '把 AgentAssetLinyao 的五台山背景改成峨眉山，重新生成，人物不变'
    response = client.post(f'/api/v1/projects/{project}/agent/sessions/{session}/messages',
        headers=creator_headers, json={'content': message})
    assert response.status_code == 202, response.text
    task = response.json()['task']
    assert task['task_type'] == 'asset_image_generation'
    assert task['request_payload']['image_edit_instruction'] == message
    assert task['request_payload']['image_edit_reference_url'] == original_url
    assert not task['request_payload'].get('asset_parent_waiting')
    gateway = FakeAssetImageGateway()
    assert asyncio.run(process_task(task['id'], gateway_factory=lambda _: gateway))
    result = client.get(f'/api/v1/tasks/{task["id"]}', headers=creator_headers).json()
    assert result['status'] == 'succeeded', result.get('error_message')
    request = gateway.requests[0]
    assert request.generation_mode == 'image_to_image'
    assert request.reference_image_urls and message in request.prompt
    updated = next(a for a in client.get(asset_url, headers=creator_headers).json() if a['id'] == asset['id'])
    assert message in updated['generation_prompt']
    assert updated['version'] > patched.json()['version']
