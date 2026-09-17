import asyncio
from types import SimpleNamespace

import pytest

from app.services.source_timeline import contract, extract, run_validated, validate_script, validate_shots


@pytest.mark.parametrize("text,expected", [
    ("镜头一 0–1.5s：冲刺\n镜头二 1.5—3秒：格挡", [(0, 1.5), (1.5, 3)]),
    ("【00—03秒】开场\n【03—11秒】对白", [(0, 3), (3, 11)]),
    ("00:00:00,000 --> 00:00:03,500\n开场", [(0, 3.5)]),
    ("00：03～01：10 追击", [(3, 70)]),
    ("0分03秒至1分10秒：追击", [(3, 70)]),
    ("第1-20章，年龄20-30，16:9画幅", []),
])
def test_time_formats(text, expected):
    assert [(s.start, s.end) for s in extract(text)] == expected


def test_script_cannot_silently_drop_original_timing():
    source = "0-8秒：进攻\n8-16秒：反击"
    validate_script(source, "00:00—00:08 进攻\n00:08—00:16 反击")
    with pytest.raises(ValueError, match="遗漏"):
        validate_script(source, "场一：双方打斗")
    assert "进攻" in contract(source)
    assert contract("普通小说内容") == ""


def shots(*durations):
    return [SimpleNamespace(duration_seconds=d) for d in durations]


def test_board_must_match_total_and_internal_boundaries():
    source = "0-8秒：进攻\n8-16秒：反击"
    validate_shots(source, shots(4, 4, 8))
    with pytest.raises(ValueError, match="总时长"):
        validate_shots(source, shots(4, 4))
    with pytest.raises(ValueError, match="边界"):
        validate_shots(source, shots(6, 10))


def test_overlapping_dialogue_is_not_added_to_shot_duration():
    source = "0-10秒：追逐\n2-6秒：旁白"
    validate_shots(source, shots(10))
    assert len(extract(source)) == 2
    with pytest.raises(ValueError, match="总时长"):
        validate_shots(source, shots(14))


def test_retry_corrects_timing_and_exhaustion_is_explicit():
    from app.services.agent_runtime import AgentRuntimeRequest
    request = AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="task",
        prompt="原文", system_prompt="约束", model_binding={}, prompt_versions={}, skill_versions={},
        skills=[], memory_context=[])
    requests = []

    class Runtime:
        async def run(self, req):
            requests.append(req)
            return SimpleNamespace(final_response="无时间" if len(requests) == 1 else "0-8秒：进攻")

    result = asyncio.run(run_validated(request, Runtime, lambda text: validate_script("0-8秒：进攻", text)))
    assert result.final_response == "0-8秒：进攻"
    assert len(requests) == 2
    assert "校验失败" in requests[1].prompt
    assert request.prompt == "原文"
    requests.clear()
    with pytest.raises(RuntimeError, match="已尝试3次"):
        asyncio.run(run_validated(request, Runtime, lambda text: validate_script("0-9秒：进攻", text)))
    assert len(requests) == 3


def test_pasted_script_timing_reaches_runtime_and_persists_after_retry(client, creator_headers, admin_headers):
    import json
    from app.services.agent_runtime import AgentRuntimeResponse
    from app.services.task_worker import process_task

    provider = client.get("/api/v1/admin/providers", headers=admin_headers).json()[0]["id"]
    assert client.patch(f"/api/v1/admin/providers/{provider}", headers=admin_headers,
                        json={"api_key": "test-timing-key"}).status_code == 200
    project = client.get("/api/v1/projects", headers=creator_headers).json()[1]["id"]
    source = "第一章 雨夜\n0-8秒：林遥推门。\n8-16秒：旧相机回卷。"
    imported = client.post(f"/api/v1/projects/{project}/sources/import", headers=creator_headers,
                           data={"mode": "script", "source_name": "时间轴测试", "pasted_text": source})
    assert imported.status_code in (200, 201), imported.text
    chapter = imported.json()["chapters"][0]["id"]
    path = f"/api/v1/projects/{project}/chapters/{chapter}/scripts"
    task = client.post(path + "/generate", headers=creator_headers, json={})
    assert task.status_code == 202, task.text
    calls = []

    class Runtime:
        async def run(self, request):
            calls.append(request)
            assert "原始剧本时间轴约束" in request.system_prompt
            assert "8—16秒" in request.system_prompt
            content = "场一：林遥推门，相机回卷。" if len(calls) == 1 else source
            return AgentRuntimeResponse(session_id=request.session_id,
                final_response=json.dumps({"title": "雨夜", "content": content, "review_notes": ""}),
                finish_reason="completed", events=[], manifest={})

    asyncio.run(process_task(task.json()["id"], runtime_factory=Runtime))
    status = client.get(f"/api/v1/tasks/{task.json()['id']}", headers=creator_headers).json()
    assert status["status"] == "succeeded", status
    assert len(calls) == 2
    versions = client.get(path, headers=creator_headers).json()
    assert versions[0]["content"] == source
