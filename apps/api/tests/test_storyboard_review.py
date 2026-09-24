import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.services.agent_runtime import AgentRuntimeRequest
from app.services.storyboard_review import parse_review, review_board, review_units


def request():
    return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
        prompt="项目模型能力", system_prompt="导演与画风手册", model_binding={}, prompt_versions={},
        skill_versions={}, skills=[], memory_context=[])


def shots(count=18):
    return [{"order_index": i, "title": f"镜头{i}", "duration_seconds": 8,
             "scene_description": "雨夜街道", "action_description": f"动作{i}",
             "image_prompt": f"首帧{i}", "dialogue": f"台词{i}", "asset_names": [],
             "combat_plan": {"brief": "连贯攻防"}, "emotion_plan": {"brief": "情绪变化"},
             "frame_layout": {"axis": "左到右"}, "internal_shots": [{"description": "变换视角"}]}
            for i in range(1, count + 1)]


async def noop(*args):
    pass


class Runtime:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []

    async def run(self, req):
        unit = json.loads(req.prompt.split("本批数据：", 1)[1].split("\n上次响应错误", 1)[0])
        self.calls.append(unit)
        if unit["shots"][0]["order_index"] == 7 and self.failures:
            self.failures -= 1
            raise RuntimeError("供应商暂不可用")
        return SimpleNamespace(final_response=json.dumps({"approved": True, "summary": "通过", "findings": []}), manifest={})


def run(runtime, state, rows=None, req=None):
    return asyncio.run(review_board(req or request(), lambda: runtime, shots=rows or shots(),
        script="一场连续的雨夜对话。", state=state, save=noop, progress=noop, concurrency=1))


def test_review_is_bounded_and_preserves_all_production_fields():
    runtime = Runtime()
    output, _ = run(runtime, {})
    assert output.approved
    assert len(runtime.calls) == 3
    assert [r["order_index"] for unit in runtime.calls for r in unit["shots"]] == list(range(1, 19))
    for unit in runtime.calls:
        assert len(unit["shots"]) <= 6 and len(unit["neighbors"]) <= 2
        assert "一场连续" in unit["source"]
        for row in unit["shots"]:
            assert all(row[k] for k in ("combat_plan", "emotion_plan", "frame_layout", "internal_shots"))


def test_transient_failure_retries_only_failed_review_batch():
    runtime = Runtime(failures=1)
    output, _ = run(runtime, {})
    assert output.approved
    assert [u["shots"][0]["order_index"] for u in runtime.calls] == [1, 7, 7, 13]


def test_exhausted_review_resumes_durable_results():
    runtime, state = Runtime(failures=3), {}
    with pytest.raises(RuntimeError, match="第 2 批审核暂未完成"):
        run(runtime, state)
    assert len(state["completed"]) == 2
    working = Runtime()
    run(working, copy.deepcopy(state))
    assert [u["shots"][0]["order_index"] for u in working.calls] == [7]


def test_repair_rechecks_only_changed_batch_and_boundary_context():
    state, runtime = {}, Runtime()
    run(runtime, state)
    rows = shots()
    rows[2]["dialogue"] = "修正内部镜头"
    next_runtime = Runtime()
    run(next_runtime, state, rows)
    assert [u["shots"][0]["order_index"] for u in next_runtime.calls] == [1]
    rows[5]["action_description"] = "边界动作变更"
    boundary = Runtime()
    run(boundary, state, rows)
    assert [u["shots"][0]["order_index"] for u in boundary.calls] == [1, 7]


def test_model_change_invalidates_review_cache():
    state = {}
    run(Runtime(), state)
    runtime = Runtime()
    run(runtime, state, req=request().model_copy(update={"model_binding": {"model": "new"}}))
    assert len(runtime.calls) == 3


def test_review_targets_exclude_comparison_shots_and_major_cannot_pass():
    result = parse_review(json.dumps({"approved": True, "summary": "需调整", "findings": [{
        "severity": "major", "location": "镜头14（对照1-13）", "shot_indices": [14],
        "context_shot_indices": [13], "fields": ["action_description"],
        "issue": "运动方向突变", "suggestion": "仅修改14的入镜动作",}]}), {14, 15}, 18)
    assert not result["approved"]
    assert result["findings"][0]["shot_indices"] == [14]
    assert result["findings"][0]["context_shot_indices"] == [13]


@pytest.mark.parametrize("fields,expected", [
    ("asset_ids", ["asset_names"]),
    (["asset_ids", "asset_names"], ["asset_names"]),
    ("frame_layout.spatial_relations", ["frame_layout"]),
    (["emotion_plan.beats[0].emotion", "internal_shots[0].description"], ["emotion_plan", "internal_shots"]),
    ([" `dialogue` "], ["dialogue"]),
])
def test_review_field_representations_keep_exact_finding(fields, expected):
    finding = {"severity": "major", "shot_indices": [14, 16], "context_shot_indices": [13, 15],
               "fields": fields, "issue": "参考资产需调整", "suggestion": "保留人物和场景的必要引用"}
    result = parse_review(json.dumps({"approved": False, "summary": "需调整", "findings": [finding]}),
                          {13, 14, 15, 16}, 18)
    normalized = result["findings"][0]
    assert normalized["fields"] == expected
    for key in ("shot_indices", "context_shot_indices", "issue", "suggestion", "severity"):
        assert normalized[key] == finding[key]
    assert not result["approved"]


def test_unknown_field_repairs_saved_review_without_resending_shots():
    calls = []
    finding = {"severity": "major", "shot_indices": [2], "context_shot_indices": [1],
               "fields": ["camera_motion"], "issue": "摄影机越轴", "suggestion": "保持机位在轴线同侧"}

    class FieldRuntime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 1:
                value = finding
                assert "合法字段仅限" in req.prompt
            else:
                assert "审核返回格式修复" in req.prompt and "本批数据：" not in req.prompt
                assert "camera_motion" in req.prompt and "frame_layout" in req.prompt
                value = {**finding, "fields": ["frame_layout"]}
            return SimpleNamespace(final_response=json.dumps({"approved": False, "summary": "需调整",
                "findings": [value]}), manifest={})

    result, _ = asyncio.run(review_board(request(), FieldRuntime, shots=shots(3), script="场景",
        state={}, save=noop, progress=noop))
    assert len(calls) == 2 and not result.approved
    assert result.findings[0].fields == ["frame_layout"]
    assert result.findings[0].issue == finding["issue"]


def test_saved_asset_id_field_error_resumes_without_model_or_rechecking_completed_batches():
    from app.services.storyboard_review import fingerprint, REVIEW_RULES

    req, rows, state = request(), shots(), {"completed": {}, "raw": {}}
    key = fingerprint([REVIEW_RULES, req.system_prompt, req.prompt, req.model_binding,
                       req.skill_versions, req.prompt_versions, req.memory_context])
    units = review_units(rows, "一场连续的雨夜对话。")
    for i, unit in enumerate(units):
        unit_key = fingerprint([key, unit])
        if i != 2:
            state["completed"][unit_key] = {"approved": True, "summary": "通过", "findings": []}
        else:
            state["raw"][unit_key] = {"response": json.dumps({"approved": False, "summary": "需调整",
                "findings": [{"severity": "major", "shot_indices": [14, 16], "fields": "asset_ids",
                    "issue": "引用需调整", "suggestion": "调整资产绑定"},
                    {"severity": "minor", "shot_indices": [15], "fields": "frame_layout",
                    "issue": "空间快照混入运动", "suggestion": "只保留开场状态"}]}), "manifest": {}}

    class NoCalls:
        async def run(self, req):
            pytest.fail("Valid saved reviews must not be regenerated")

    result, _ = run(NoCalls(), state)
    assert not result.approved and len(result.findings) == 2
    assert result.findings[0].fields == ["asset_names"]
    assert len(state["completed"]) == 3 and not state["raw"]


@pytest.mark.parametrize("finding", [
    {"location": "整章", "shot_indices": []},
    {"location": "镜头1", "shot_indices": [1]},
    {"location": "镜头14", "shot_indices": [14], "fields": ["not_a_field"]},
])
def test_invalid_review_scope_is_retried_not_silently_widened(finding):
    with pytest.raises(ValueError):
        parse_review(json.dumps({"approved": False, "summary": "错误", "findings": [{
            "severity": "major", "issue": "问题", "suggestion": "修复", **finding}]}), {14}, 18)


def test_unmatched_source_is_not_automatically_declared_missing():
    rows = shots(2)
    units = review_units(rows, "场1｜外景\n没有相似字词的第一场\n场2｜内景\n另一场的正文")
    assert any(unit["coverage_only"] for unit in units)
    assert all(unit["shots"] for unit in units)
    runtime = Runtime()
    output, _ = asyncio.run(review_board(request(), lambda: runtime, shots=rows,
        script="场1｜外景\n没有相似字词的第一场\n场2｜内景\n另一场的正文",
        state={}, save=noop, progress=noop))
    assert output.approved


def test_previous_asset_state_is_retrieved_across_distant_shots():
    rows = shots(18)
    rows[0]["asset_names"] = rows[15]["asset_names"] = ["女主"]
    units = review_units(rows, "一场连续的雨夜对话。")
    assert units[-1]["asset_history"][0]["order_index"] == 1


def test_empty_board_cannot_pass_review():
    with pytest.raises(ValueError, match="没有可审核"):
        asyncio.run(review_board(request(), Runtime, shots=[], script="剧本", state={}, save=noop, progress=noop))


def test_long_shot_is_split_losslessly_instead_of_overflowing_context():
    row = shots(1)[0]
    row["action_description"] = "完整连续战斗动作与台词" * 4000
    units = review_units([row], "连续交锋", budget=14000)
    assert len(units) > 1
    restored = "".join(u["shots"][0]["review_fragment"] for u in units)
    assert json.loads(restored) == row
    assert all(len(json.dumps(u, ensure_ascii=False)) < 14000 for u in units)


def test_parallel_review_is_bounded_and_saves_completed_batches_after_failure():
    async def scenario():
        active = maximum = 0
        started = []
        saved = []
        state = {}

        class ConcurrentRuntime:
            async def run(self, req):
                nonlocal active, maximum
                unit = json.loads(req.prompt.split("本批数据：", 1)[1].split("\n上次响应错误", 1)[0])
                index = unit["shots"][0]["order_index"]
                started.append(index)
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0)
                active -= 1
                if index == 7:
                    raise RuntimeError("无法连接")
                return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[]}', manifest={})

        async def save(value):
            saved.append(copy.deepcopy(value))
            await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="第 2 批"):
            await review_board(request(), ConcurrentRuntime, shots=shots(), script="场景",
                               state=state, save=save, progress=noop)
        assert maximum == 2
        assert started.count(1) == 1 and started.count(7) == 3
        assert saved and state["completed"]
        resumed = Runtime()
        await review_board(request(), lambda: resumed, shots=shots(), script="场景",
                           state=state, save=save, progress=noop)
        assert all(u["shots"][0]["order_index"] != 1 for u in resumed.calls)

    asyncio.run(scenario())


def test_malformed_review_is_repaired_without_resending_storyboard():
    calls, saved = [], []
    finding = {"severity": "major", "shot_indices": [2], "fields": ["dialogue"],
               "location": "镜头2", "issue": "台词遗漏", "suggestion": "补回原台词"}

    class FormatRuntime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 1:
                text = '{"approved":false,"summary":"需修复","findings":[' + json.dumps(finding) + ',]}'
            else:
                assert "审核返回格式修复" in req.prompt
                assert "本批数据：" not in req.prompt
                assert "动作1" not in req.prompt
                assert not req.memory_context and not req.project_files and not req.skills
                text = json.dumps({"approved": False, "summary": "需修复", "findings": [finding]})
            return SimpleNamespace(final_response=text, manifest={})

    async def save(value):
        saved.append(copy.deepcopy(value))

    state = {}
    result, _ = asyncio.run(review_board(request(), FormatRuntime, shots=shots(3), script="场景",
                                         state=state, save=save, progress=noop))
    assert not result.approved and result.findings[0].shot_indices == [2]
    assert len(calls) == 2
    assert any(s.get("raw") for s in saved)
    assert not state["raw"]


def test_review_restart_repairs_saved_response_instead_of_repeating_review():
    state, calls = {}, []

    class FailingRuntime:
        async def run(self, req):
            calls.append(req)
            if len(calls) > 1:
                raise RuntimeError("格式修复接口暂不可用")
            return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[],}', manifest={})

    with pytest.raises(RuntimeError, match="第 1 批审核暂未完成"):
        asyncio.run(review_board(request(), FailingRuntime, shots=shots(3), script="场景",
                                 state=state, save=noop, progress=noop))
    assert state["raw"] and len(calls) == 3

    class RecoveredRuntime:
        async def run(self, req):
            assert "审核返回格式修复" in req.prompt
            assert "本批数据：" not in req.prompt
            return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[]}', manifest={})

    result, _ = asyncio.run(review_board(request(), RecoveredRuntime, shots=shots(3), script="场景",
                                         state=copy.deepcopy(state), save=noop, progress=noop))
    assert result.approved


def test_wrong_review_target_requires_evidence_not_format_repair():
    calls = []

    class WrongTargetRuntime:
        async def run(self, req):
            calls.append(req)
            assert "本批数据：" in req.prompt
            text = json.dumps({"approved": False, "summary": "需修复", "findings": [{
                "severity": "major", "shot_indices": [9], "location": "镜头9",
                "issue": "台词遗漏", "suggestion": "补回台词", "fields": ["dialogue"]}]})
            return SimpleNamespace(final_response=text, manifest={})

    with pytest.raises(RuntimeError, match="本批待审核镜号"):
        asyncio.run(review_board(request(), WrongTargetRuntime, shots=shots(3), script="场景",
                                 state={}, save=noop, progress=noop))
    assert len(calls) == 3


def test_cancelled_review_does_not_schedule_more_batches():
    calls = []

    class CancelledRuntime:
        async def run(self, req):
            calls.append(req)
            raise RuntimeError("分镜审核任务已停止")

    with pytest.raises(RuntimeError, match="已停止"):
        asyncio.run(review_board(request(), CancelledRuntime, shots=shots(), script="场景",
                                 state={}, save=noop, progress=noop, concurrency=1))
    assert len(calls) == 1


def test_format_repair_cannot_silently_approve_a_rejected_batch():
    calls = []
    finding = {"severity": "major", "shot_indices": [2], "fields": ["dialogue"],
               "location": "镜头2", "issue": "台词遗漏", "suggestion": "补回原台词"}

    class Runtime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 1:
                text = '{"approved":false,"summary":"需修复","findings":[' + json.dumps(finding) + ',]}'
            elif len(calls) == 2:
                assert "审核返回格式修复" in req.prompt
                text = '{"approved":true,"summary":"通过","findings":[]}'
            else:
                assert "本批数据：" in req.prompt
                text = json.dumps({"approved": False, "summary": "确实遗漏", "findings": [finding]})
            return SimpleNamespace(final_response=text, manifest={})

    result, _ = asyncio.run(review_board(request(), Runtime, shots=shots(3), script="场景",
                                         state={}, save=noop, progress=noop))
    assert len(calls) == 3
    assert not result.approved and result.findings[0].issue == "台词遗漏"


def test_provider_outage_does_not_retry_every_remaining_review_batch():
    calls = []

    class OfflineRuntime:
        async def run(self, req):
            calls.append(req)
            raise RuntimeError("供应商离线")

    with pytest.raises(RuntimeError, match="审核暂未完成"):
        asyncio.run(review_board(request(), OfflineRuntime, shots=shots(60), script="场景",
                                 state={}, save=noop, progress=noop, concurrency=1))
    assert len(calls) == 6  # Two batches prove a broad outage; don't spend 30 calls.


@pytest.mark.parametrize("incomplete", [
    "I'll start by reading the shot file under review, then trace the source script and context.",
    '{"approved":false,"summary":"尚未完成读取","findings":[]}',
    '{"message":"先读取分镜"}',
])
def test_incomplete_review_retries_with_evidence_not_json_formatter(incomplete):
    calls, saved = [], []

    class IncompleteRuntime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 1:
                text = incomplete
            else:
                assert "审核返回格式修复" not in req.prompt
                assert "本批审核未完成" in req.prompt
                assert "动作1" in req.prompt
                text = json.dumps({"approved": False, "summary": "需修复", "findings": [{
                    "severity": "major", "shot_indices": [1], "fields": ["dialogue"],
                    "issue": "原台词遗漏", "suggestion": "补回原台词"}]})
            return SimpleNamespace(final_response=text, manifest={})

    async def save(value):
        saved.append(copy.deepcopy(value))

    state = {}
    output, _ = asyncio.run(review_board(request(), IncompleteRuntime, shots=shots(3), script="原台词",
                                        state=state, save=save, progress=noop))
    assert not output.approved
    assert output.findings[0].shot_indices == [1]
    assert len(calls) == 2
    assert all(not value.get("raw") for value in saved)


def test_saved_planning_response_is_discarded_without_losing_completed_reviews():
    state = {}
    run(Runtime(), state)
    key = next(iter(state["completed"]))
    state["completed"].pop(key)
    state["raw"][key] = {"response": "I'll start by reading the shot file and the source script.", "manifest": {}}
    calls = []

    class RecoveryRuntime:
        async def run(self, req):
            calls.append(req)
            assert "审核返回格式修复" not in req.prompt
            assert "本批审核未完成" in req.prompt
            return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[]}', manifest={})

    output, _ = run(RecoveryRuntime(), state)
    assert output.approved
    assert len(calls) == 1
    assert len(state["completed"]) == 3
    assert not state["raw"]


def test_persistent_planning_only_response_cannot_be_approved():
    calls = []

    class PlanningRuntime:
        async def run(self, req):
            calls.append(req)
            assert "审核返回格式修复" not in req.prompt
            return SimpleNamespace(final_response="先读取分镜文件。", manifest={})

    state = {}
    with pytest.raises(RuntimeError, match="尚未给出审核结论"):
        run(PlanningRuntime(), state, rows=shots(3))
    assert len(calls) == 3
    assert not state["completed"] and not state["raw"]


def test_review_attempt_reports_tool_activity_and_cancels_on_deadline(monkeypatch):
    from app.services import storyboard_review as module
    monkeypatch.setattr(module, "REVIEW_ATTEMPT_TIMEOUT", 0.04)
    monkeypatch.setattr(module, "REVIEW_PROGRESS_INTERVAL", 0.01)
    messages, cancelled = [], []

    class SlowRuntime:
        async def run_stream(self, req, on_event):
            await on_event({"type": "MODEL_CALL_START"})
            await on_event({"type": "TOOL_CALL_START", "tool_call_name": "Read"})
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    async def progress(message):
        messages.append(message)

    with pytest.raises(RuntimeError, match="单批审核超过"):
        asyncio.run(module.run_review_attempt(SlowRuntime(), request(), progress, "镜头1"))
    assert cancelled == [True]
    assert any("Read" in message and "模型 1 轮 / 工具 1 次" in message for message in messages)


def test_review_supports_four_independent_units_without_duplicate_calls():
    from app.services.retrieval_context import automatic_request
    async def scenario():
        active = maximum = 0
        reached = asyncio.Event()
        calls = []

        class Runtime:
            async def run(self, req):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                files = {file.id: file for file in req.project_files}
                calls.append(json.loads(files["retrieval-review-shot"].content)[0]["order_index"])
                if active == 4:
                    reached.set()
                await asyncio.wait_for(reached.wait(), 1)
                active -= 1
                return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[]}', manifest={})

        result, _ = await review_board(automatic_request(request(), rules="规则"), Runtime,
            shots=shots(8), script="原文", state={}, save=noop, progress=noop, concurrency=4)
        assert maximum == 4 and sorted(calls) == list(range(1, 9))
        assert result.approved
    asyncio.run(scenario())
