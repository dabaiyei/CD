import asyncio
import json
from types import SimpleNamespace

from app.services.agent_runtime import AgentRuntimeRequest
from app.services.retrieval_context import automatic_request
from app.services.storyboard_review import review_board


def request():
    return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
        prompt="合法时长8秒，人物参考模型契约", system_prompt="硬约束", model_binding={},
        prompt_versions={}, skill_versions={}, skills=[], memory_context=[])


def test_huge_source_is_only_transported_as_file_and_rules_remain_available():
    source = "全章不可内联" * 10000
    req = automatic_request(request().model_copy(update={"prompt": source}), rules="合法时长4至15秒")
    assert len(req.prompt) < 1000
    assert "全章不可内联" not in req.system_prompt
    assert any(file.content == source for file in req.project_files)
    assert all(not file.editable for file in req.project_files)
    assert req.tool_mode == "retrieval"


def test_production_review_uses_one_shot_tool_context_and_reuses_unchanged_results():
    rows = [{"order_index": i, "title": f"镜{i}", "dialogue": f"台词{i}", "duration_seconds": 8,
             "image_prompt": "人物"} for i in range(1, 5)]
    state, calls = {}, []
    req = automatic_request(request(), rules="规则")
    class Runtime:
        async def run(self, current):
            assert current.tool_mode == "retrieval"
            files = {file.id: file for file in current.project_files}
            shot = json.loads(files["retrieval-review-shot"].content)
            assert len(shot) == 1
            packet = current.prompt.split("<current-shot-evidence>\n", 1)[1].split("\n</current-shot-evidence>", 1)[0]
            assert len(packet) <= 12000
            assert json.loads(packet)["shots"] == shot
            calls.append(shot[0]["order_index"])
            return SimpleNamespace(final_response=json.dumps({"approved": True, "summary": "通过", "findings": []}), manifest={})
    async def noop(*args):
        pass
    async def run():
        return await review_board(req, Runtime, shots=rows, script="原文证据", state=state,
            save=noop, progress=noop, concurrency=2)
    asyncio.run(run())
    assert sorted(calls) == [1, 2, 3, 4]
    calls.clear()
    state["completed"]["obsolete-board-fingerprint"] = {"approved": True, "summary": "过期", "findings": []}
    asyncio.run(run())
    assert calls == []
    assert len(state["completed"]) == 4
    rows[1]["dialogue"] = "修复台词"
    asyncio.run(run())
    assert sorted(calls) == [1, 2, 3]  # 本镜和相邻衔接受影响，第四镜仍复用。


def test_large_review_evidence_stays_complete_in_files():
    detail = "完整镜头证据" * 2300
    rows = [{"order_index": 1, "title": "长镜头", "action_description": detail,
             "duration_seconds": 8}]
    calls = []

    class Runtime:
        async def run(self, current):
            assert "<current-shot-evidence>" not in current.prompt
            files = {file.id: file for file in current.project_files}
            content = json.loads(files["retrieval-review-shot"].content)
            assert content[0]["action_description"] == detail
            calls.append(current)
            return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[]}', manifest={})

    async def noop(*args):
        pass

    asyncio.run(review_board(automatic_request(request(), rules="规则"), Runtime,
        shots=rows, script="原文", state={}, save=noop, progress=noop))
    assert len(calls) == 1


def test_single_shot_repair_mounts_only_local_operation_without_inline_board():
    from app.services.storyboard_generation import generate
    rows = [{"title": f"镜头{i}", "dialogue": f"私有台词{i}", "duration_seconds": 8,
             "image_prompt": "人物首帧", "asset_names": []} for i in range(1, 31)]
    calls = []
    class Runtime:
        async def run(self, req):
            calls.append(req)
            assert req.tool_mode == "retrieval"
            assert "私有台词" not in req.prompt
            current = next(file for file in req.project_files if file.id == "retrieval-repair-shot")
            assert json.loads(current.content)["title"] == "镜头23"
            return SimpleNamespace(final_response='{"fields":{"dialogue":"正确台词"}}', manifest={})
    async def noop(*args):
        pass
    output, _ = asyncio.run(generate(automatic_request(request(), rules="规则"), Runtime,
        plan=[], durations=[8], state={}, save=noop, progress=noop, repair_shots=rows,
        findings=[{"shot_indices": [23], "fields": ["dialogue"], "severity": "major", "issue": "台词错误"}]))
    assert len(calls) == 1
    repaired = json.loads(output)["shots"]
    assert len(repaired) == 30
    assert repaired[22]["dialogue"] == "正确台词"
    assert repaired[0]["dialogue"] == "私有台词1"
