import asyncio
import copy
import json
import re
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures('jev_disabled')

from app.services.agent_runtime import AgentRuntimeRequest
from app.services.storyboard_generation import (
    assemble_repaired_board,
    allocate_timeline,
    complete_output,
    coverage_finding,
    decode_repair_reply,
    decode_shots,
    finding_fields,
    generate,
    match_existing_shots,
    merge_shot_patch,
    parse_finding_targets,
    parse_user_targets,
    scenes_for_coverage,
    segment_offset,
    segment_script,
    split_shot_finding,
)


def shot(index, duration=8):
    return {"order_index": index, "title": f"镜头{index}", "duration_seconds": duration,
            "image_prompt": "雨夜，保持人物身份", "action_description": "推门后接转身",
            "dialogue": "别走", "asset_names": []}


def test_insertions_remap_planning_references_without_mutating_checkpoint_or_dialogue():
    valid = {str(i): shot(i) for i in range(1, 4)}
    valid["3"].update(action_description="承接镜2，转至镜头 3；镜99未知",
        dialogue="他说镜2很好", frame_layout={"notes": "接镜头2"})
    original = copy.deepcopy(valid)
    inserted = {"1": [{**shot(1), "action_description": "后接镜2"}]}
    rows = assemble_repaired_board([1, 2, 3], valid, inserted)
    assert rows[3]["action_description"] == "承接镜3，转至镜头 4；镜99未知"
    assert rows[3]["frame_layout"]["notes"] == "接镜头3"
    assert rows[1]["action_description"] == "后接镜3"
    assert rows[3]["dialogue"] == "他说镜2很好"
    assert valid == original
    assert assemble_repaired_board([1, 2, 3], valid, inserted) == rows


def request():
    return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
        prompt="剧本和模型能力", system_prompt="导演约束", model_binding={}, prompt_versions={},
        skill_versions={}, skills=[], memory_context=[])


def test_targeted_repair_sees_adjacent_source_not_just_lexical_match(monkeypatch):
    import app.services.storyboard_generation as module
    segments = [{"key": str(i), "label": f"场{i}", "content": f"场{i}原文"} for i in range(1, 4)]
    segments[1]["content"] = "五颗球，白球回弹"
    segments[2]["content"] = "远距离击球，红球落袋，仅剩两颗球"
    monkeypatch.setattr(module, "segment_script", lambda _: segments)
    monkeypatch.setattr(module, "match_existing_shots", lambda _, rows: {"1": rows[:1], "2": rows[1:2], "3": rows[2:]})
    class Runtime:
        async def run(self, req):
            assert "source_context" in req.prompt
            assert "仅剩两颗球" in req.prompt
            assert "不是已确认" in req.prompt
            return SimpleNamespace(final_response='{"fields":{"action_description":"远距离击球，红球落袋，仅剩两颗球"}}', manifest={})
    async def noop(*args):
        pass
    result, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={},
        save=noop, progress=noop, script="剧本", repair_shots=[shot(i) for i in range(1, 4)],
        findings=[{"shot_indices": [2], "fields": ["action_description"], "severity": "major",
                   "issue": "球数与原文不符", "suggestion": "核对原文修复"}]))
    assert "仅剩两颗球" in json.loads(result)["shots"][1]["action_description"]


def test_automatic_repair_skips_minor_and_preserves_previous_patches():
    rows = [shot(i) for i in range(1, 4)]
    state = {"valid": {"1": {**rows[0], "dialogue": "已保存的修复"}}, "repaired_indices": [1]}
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            assert "只修复第 3 镜" in req.prompt
            return SimpleNamespace(final_response='{"fields":{"dialogue":"主要问题已修复"}}', manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
        save=noop, progress=noop, repair_shots=rows, repair_blocking_only=True,
        findings=[{"shot_indices": [i], "fields": ["dialogue"], "severity": severity, "issue": "问题"}
                  for i, severity in [(1, "major"), (2, "minor"), (3, "major")]]))
    output = json.loads(result)["shots"]
    assert len(calls) == 1
    assert output[0]["dialogue"] == "已保存的修复"
    assert output[1]["dialogue"] == rows[1]["dialogue"]
    assert output[2]["dialogue"] == "主要问题已修复"


def test_spatial_patch_updates_matching_action_without_overwriting_dialogue():
    original = [shot(1), shot(2)]
    class Runtime:
        async def run(self, req):
            assert "同一事实的关联同步字段" in req.prompt
            return SimpleNamespace(final_response=json.dumps({"fields": {
                "image_prompt": "同一机位0.9米平视", "action_description": "从0.9米平视机位开始跟拍",
                "dialogue": "不得改写的台词", "title": "不得改写的标题"}}), manifest={})
    async def noop(*args):
        pass
    output, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={},
        save=noop, progress=noop, repair_shots=original, findings=[{"shot_indices": [1],
        "fields": ["image_prompt"], "severity": "major", "issue": "同一机位高度矛盾"}]))
    rows = json.loads(output)["shots"]
    assert rows[0]["action_description"] == "从0.9米平视机位开始跟拍"
    assert rows[0]["dialogue"] == original[0]["dialogue"]
    assert rows[0]["title"] == original[0]["title"]
    assert rows[1]["image_prompt"] == original[1]["image_prompt"]


def test_exact_sentence_patch_preserves_other_actions_and_rejects_ambiguous_edits():
    original = {**shot(6), "action_description": "0–3秒：机位0.9米平视，推门。3–8秒：停下说话。"}
    output = merge_shot_patch(original, [{"order_index": "006", "edits": [{
        "field": "action_description", "old": "机位0.9米平视", "new": "机位0.6米仰视"}]}],
        6, ["action_description"])
    assert output["action_description"] == "0–3秒：机位0.6米仰视，推门。3–8秒：停下说话。"
    assert original["action_description"] == "0–3秒：机位0.9米平视，推门。3–8秒：停下说话。"
    assert output["dialogue"] == original["dialogue"]
    for edits in ([{"field": "action_description", "old": "不存在", "new": "新词"}],
                  [{"field": "action_description", "old": "秒", "new": "时"}],
                  [{"field": "dialogue", "old": "别走", "new": "改词"}]):
        with pytest.raises(ValueError):
            merge_shot_patch(original, [{"edits": edits}], 6, ["action_description"])


def test_obsolete_neighbor_finding_can_preserve_shot_without_approving_review():
    original = shot(1)
    output = merge_shot_patch(original, [{"unchanged": True, "reason": "邻镜已移除重复反应"}], 1, ["action_description"])
    assert output == original and output is not original
    assert "approved" not in output
    with pytest.raises(ValueError):
        merge_shot_patch(original, [{"unchanged": True, "reason": "", "fields": {"dialogue": "改词"}}], 1, ["dialogue"])


def test_action_patch_cannot_silently_extend_clip_timing():
    original = shot(1, 6)
    with pytest.raises(ValueError, match="时间轴超过"):
        merge_shot_patch(original, [{"fields": {"action_description": "0–6秒：推门。6–10秒：转身。"}}],
                         1, ["action_description"])
    output = merge_shot_patch(original, [{"fields": {
        "action_description": "0–6秒：推门。6–10秒：转身。", "duration_seconds": 10}}],
        1, ["action_description", "duration_seconds"])
    assert output["duration_seconds"] == 10


def test_invalid_sentence_patch_repairs_saved_output_with_only_field_evidence():
    calls, saved = [], []
    state = {}
    original = [shot(1), {**shot(2), "dialogue": "邻镜内容不应重复发送"}]
    class Runtime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 1:
                return SimpleNamespace(final_response=json.dumps({"edits": [{
                    "field": "image_prompt", "old": "自行改写的原句", "new": "正确机位"}]}), manifest={})
            assert "邻镜内容不应重复发送" not in req.prompt
            assert "自行改写的原句" in req.prompt
            assert original[0]["image_prompt"] in req.prompt
            assert req.tool_mode == "none"
            return SimpleNamespace(final_response=json.dumps({"edits": [{
                "field": "image_prompt", "old": original[0]["image_prompt"], "new": "正确机位"}]}), manifest={})
    async def save(value):
        saved.append(copy.deepcopy(value))
    async def noop(*args):
        pass
    result, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
        save=save, progress=noop, repair_shots=original,
        findings=[{"shot_indices": [1], "fields": ["image_prompt"], "issue": "机位错误"}]))
    assert len(calls) == 2
    assert any(value.get("repair_outputs", {}).get("1", {}).get("error") for value in saved)
    assert not state["repair_outputs"]
    assert json.loads(result)["shots"][0]["image_prompt"] == "正确机位"


def test_incomplete_response_does_not_masquerade_as_user_cancellation():
    calls = []
    efforts = []
    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            efforts.append(req.model_binding.get("reasoning_effort"))
            if len(calls) == 1:
                raise RuntimeError("模型未返回正文；已停止内部重复调用，请重试本次请求")
            return SimpleNamespace(final_response='{"fields":{"dialogue":"修正台词"}}', manifest={})
    async def noop(*args):
        pass
    from app.services.retrieval_context import automatic_request
    req = automatic_request(request().model_copy(update={"model_binding": {
        "api_mode": "responses", "reasoning_effort": "high"}}), rules="只修正错误台词")
    result, _ = asyncio.run(generate(req, Runtime, plan=[], durations=[8], state={},
        save=noop, progress=noop, repair_shots=[shot(1)],
        findings=[{"shot_indices": [1], "fields": ["dialogue"], "issue": "错误台词"}]))
    assert len(calls) == 2
    assert efforts == ["low", "none"]
    assert json.loads(result)["shots"][0]["dialogue"] == "修正台词"


def test_independent_repairs_overlap_and_keep_success_on_failure():
    async def scenario():
        started = asyncio.Event()
        state = {}
        class Runtime:
            async def run(self, req):
                if "只修复第 1 镜" in req.prompt:
                    await asyncio.wait_for(started.wait(), 1)
                    raise RuntimeError("本镜不可用")
                started.set()
                return SimpleNamespace(final_response='{"fields":{"dialogue":"已修复"}}', manifest={})
        async def noop(*args):
            pass
        with pytest.raises(RuntimeError, match="第 1 镜"):
            await generate(request(), Runtime, plan=[], durations=[8], state=state,
                save=noop, progress=noop, concurrency=2, batch_attempts=1,
                repair_shots=[shot(i) for i in range(1, 4)], findings=[
                    {"shot_indices": [i], "fields": ["dialogue"], "issue": f"修正{i}"} for i in [1, 3]])
        assert state["valid"]["3"]["dialogue"] == "已修复"
        assert state["repaired_indices"] == [3]
    asyncio.run(scenario())


def test_fast_repair_slot_continues_without_waiting_for_slow_sibling():
    async def scenario():
        fifth_started = asyncio.Event()
        class Runtime:
            async def run(self, req):
                if "只修复第 1 镜" in req.prompt:
                    await asyncio.wait_for(fifth_started.wait(), 2)
                if "只修复第 5 镜" in req.prompt:
                    fifth_started.set()
                return SimpleNamespace(final_response='{"fields":{"dialogue":"修好"}}', manifest={})
        async def noop(*args):
            pass
        result, _ = await generate(request(), Runtime, plan=[], durations=[8], state={},
            save=noop, progress=noop, concurrency=2, batch_attempts=1,
            repair_shots=[shot(i) for i in range(1, 6)], findings=[
                {"shot_indices": [i], "fields": ["dialogue"], "issue": f"修正{i}"} for i in [1, 3, 5]])
        assert fifth_started.is_set()
        assert [row["dialogue"] for row in json.loads(result)["shots"]] == ["修好", "别走", "修好", "别走", "修好"]
    asyncio.run(scenario())


def test_adjacent_repair_chain_cannot_overtake_pending_predecessor():
    calls = []
    class Runtime:
        async def run(self, req):
            index = int(re.search(r"只修复第 (\d+) 镜", req.prompt)[1])
            calls.append(index)
            if index > 1:
                assert f"已修复{index - 1}" in req.prompt
            await asyncio.sleep(.01)
            return SimpleNamespace(final_response=json.dumps({"fields": {"dialogue": f"已修复{index}"}}), manifest={})
    async def noop(*args):
        pass
    asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={}, save=noop, progress=noop,
        concurrency=3, repair_shots=[shot(i) for i in range(1, 4)],
        findings=[{"shot_indices": [i], "fields": ["dialogue"], "issue": "修正对白"} for i in range(1, 4)]))
    assert calls == [1, 2, 3]


def test_chinese_duration_suggestion_permits_timeline_extension():
    fields = finding_fields([{"fields": ["action_description"],
        "suggestion": "将动作链纳入本镜并改用能容纳它的合法档位（如9—10秒）重新分配时间轴"}])
    assert "duration_seconds" in fields
    assert "duration_seconds" not in finding_fields([{"fields": ["action_description"],
        "suggestion": "不得延长时长，保留原来的4秒"}])


def test_missing_action_cannot_be_fixed_only_in_scene_text():
    from app.services.storyboard_generation import validate_action_repair
    original = shot(1)
    issue = {"severity": "major", "fields": ["action_description"],
        "issue": "关键动作链缺失", "suggestion": "补入动作链"}
    with pytest.raises(ValueError, match="action_description"):
        validate_action_repair(original, {**original, "scene_description": "完成动作"}, [issue], {"fields": {}})
    validate_action_repair(original, {**original, "action_description": "补齐动作"}, [issue], {"fields": {}})


def test_missing_beat_is_inserted_once_and_untouched_shots_survive_resume():
    state, calls = {}, []
    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            assert "insert_after" in req.prompt
            return SimpleNamespace(final_response=json.dumps({"fields": {"dialogue": "衔接"},
                "insert_after": [{**shot(1), "title": "补回告白", "dialogue": "原文告白"}]}), manifest={})
    async def noop(*args):
        pass
    kwargs = dict(plan=[], durations=[8], state=state, save=noop, progress=noop,
        repair_shots=[shot(1), shot(2)], findings=[{"shot_indices": [1, 2],
        "fields": ["dialogue"], "issue": "在第一镜后补镜，缺少告白"}])
    # Only the first target owns an insertion; the other target has a normal patch.
    kwargs["findings"][0]["shot_indices"] = [1]
    result, _ = asyncio.run(generate(request(), Runtime, **kwargs))
    rows = json.loads(result)["shots"]
    assert [row["title"] for row in rows] == ["镜头1", "补回告白", "镜头2"]
    assert rows[2]["dialogue"] == "别走"
    resumed, _ = asyncio.run(generate(request(), Runtime, **kwargs))
    assert json.loads(resumed) == json.loads(result)
    assert len(calls) == 1


def test_overloaded_shot_can_split_without_rewriting_other_shots():
    state = {}
    class Runtime:
        async def run(self, req):
            assert "本镜因时长过载允许局部拆镜" in req.prompt
            return SimpleNamespace(final_response=json.dumps({
                "fields": {"dialogue": "第一句", "action_description": "前半段"},
                "insert_after": [{**shot(1), "dialogue": "第二句", "action_description": "后半段"}]}), manifest={})
    async def noop(*args):
        pass
    rows = [{**shot(1), "dialogue": "第一句\n第二句"}, shot(2)]
    output, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
        save=noop, progress=noop, repair_shots=rows, findings=[{"shot_indices": [1],
        "fields": ["duration_seconds"], "severity": "blocking", "issue": "台词时长过载，12秒装不进",
        "suggestion": "按自然语义拆为两条独立镜头"}]))
    result = json.loads(output)["shots"]
    assert [row["dialogue"] for row in result] == ["第一句", "第二句", "别走"]
    assert result[0]["action_description"] == "前半段"
    assert result[2]["action_description"] == rows[1]["action_description"]
    assert not split_shot_finding({"issue": "时长不够", "suggestion": "无需拆镜，提高合法时长即可"})


def test_missing_dialogue_with_explicit_time_and_insertion_authorization():
    finding = {"fields": ["dialogue"], "issue": "两句OS遗漏",
        "suggestion": "安排足够时长，本镜已达12秒上限，紧邻其后新增一段承接镜"}
    assert split_shot_finding(finding)
    assert "duration_seconds" in finding_fields([finding])
    assert not split_shot_finding({"issue": "时长不足", "suggestion": "不要拆为连续小段落"})


def test_parallel_batches_finish_out_of_order_and_resume_only_failed_scene():
    async def scenario():
        state, calls, completed_order = {}, [], []
        second_started = asyncio.Event()
        fail_second = True

        class Runtime:
            async def run(self, req):
                match = re.search(r"第 (\d+)/3 个场景", req.prompt)
                assert match, req.prompt
                number = int(match[1])
                calls.append(number)
                if number == 1:
                    await asyncio.wait_for(second_started.wait(), 2)
                if number == 2:
                    second_started.set()
                    if fail_second:
                        raise RuntimeError("本批模型暂不可用")
                completed_order.append(number)
                return SimpleNamespace(final_response=json.dumps({"shots": [
                    {**shot(1), "title": f"场景{number}"}]}), manifest={})

        async def noop(*args):
            pass

        with pytest.raises(RuntimeError, match="其余批次已保存"):
            await generate(request(), Runtime, plan=[], durations=[8], state=state,
                save=noop, progress=noop, script=PROSE_CHAPTER, concurrency=2, isolate_failures=True)
        assert len(state["completed_batches"]) == 2
        assert 1 in completed_order and 3 in completed_order
        calls.clear()
        fail_second = False
        text, _ = await generate(request(), Runtime, plan=[], durations=[8], state=state,
            save=noop, progress=noop, script=PROSE_CHAPTER, concurrency=2, isolate_failures=True)
        assert calls == [2]
        rows = json.loads(text)["shots"]
        assert sorted(map(int, state["valid"])) == [1, 2, 3]
        assert [r["title"] for r in rows] == ["场景1", "场景2", "场景3"]

    asyncio.run(scenario())


def test_plan_exact_model_durations_and_boundaries():
    plan = allocate_timeline("0-15秒：连续交锋", list(range(4, 13)))
    assert sum(s["duration_seconds"] for s in plan) == 15
    assert len(plan) == 2
    assert all(4 <= s["duration_seconds"] <= 12 for s in plan)
    assert plan[0]["end_seconds"] == plan[1]["start_seconds"]
    plan = allocate_timeline("0-8秒：交锋\n8-16秒：反击", [4, 8, 12])
    assert [s["end_seconds"] for s in plan] == [8, 16]
    assert allocate_timeline("普通小说，无时间轴", [4, 8]) == []
    assert allocate_timeline("0-4秒：开头\n10-15秒：结尾", [4, 8]) == []


def test_plan_overlays_fractional_and_impossible():
    assert len(allocate_timeline("0-10秒：画面\n2-6秒：旁白", [5, 10])) == 1
    assert allocate_timeline("0-1.5秒：动作", [.5, 1.5])[0]["duration_seconds"] == 1.5
    plan = allocate_timeline("0-2秒：开头\n2-5秒：特写\n5-15秒：交锋", list(range(4, 16)))
    assert len(plan) == 1
    assert plan[0]["duration_seconds"] == 15
    assert [p["end_seconds"] for p in plan[0]["internal_shots"]] == [2, 5, 15]
    with pytest.raises(RuntimeError, match="无法.*精确组合"):
        allocate_timeline("0-3秒：整章只有3秒", [4, 8, 12])


def test_decode_fenced_and_truncated_preserves_complete_shots():
    row = shot(1)
    assert decode_shots('```json\n' + json.dumps({"shots": [row]}) + '\n```') == [row]
    assert decode_shots('{"shots":[' + json.dumps(row) + ', {"title": "未完') == [row]
    assert decode_shots('已保存文件，没有JSON') == []
    assert not complete_output('{"shots":[' + json.dumps(row) + ', {"title": "未完')
    assert complete_output(json.dumps({"shots": [row]}))


def test_invalid_middle_shot_repairs_only_that_shot_and_checkpoints_later_valid_row():
    calls, snapshots = [], []
    state = {}
    plan = allocate_timeline("0-8秒：开场\n8-16秒：进攻\n16-24秒：反击", [8])

    class Runtime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 1:
                rows = [shot(1), {**shot(2), "image_prompt": ""}, shot(3)]
            else:
                assert "仅返回第2镜" in req.prompt
                assert "1" in state["valid"] and "3" in state["valid"]
                rows = [shot(2)]
            return SimpleNamespace(final_response=json.dumps({"shots": rows}), manifest={})

    async def save(value):
        snapshots.append(copy.deepcopy(value))

    async def progress(message):
        pass

    text, _ = asyncio.run(generate(request(), Runtime, plan=plan, durations=[8],
                                  state=state, save=save, progress=progress))
    assert len(calls) == 2
    assert all(r.tool_mode == "none" and r.state_mode == "ephemeral" for r in calls)
    assert [s["dialogue"] for s in json.loads(text)["shots"]] == ["别走"] * 3
    assert len(snapshots[-1]["valid"]) == 3
    # Resuming a complete checkpoint must not ask the model to generate again.
    asyncio.run(generate(request(), Runtime, plan=plan, durations=[8],
                         state=state, save=save, progress=progress))
    assert len(calls) == 2


def test_unparseable_reply_format_repaired_without_repeating_original_prompt():
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(final_response="镜头1：雨夜，推门，8秒" if len(calls) == 1
                else json.dumps({"shots": [shot(1)]}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={},
                                    save=noop, progress=noop))
    assert len(json.loads(result)["shots"]) == 1
    assert len(calls) == 2
    assert "仅修复" in calls[1].prompt
    assert "剧本和模型能力" not in calls[1].prompt


def test_transport_failure_resumes_previous_batch_without_regeneration():
    state, calls = {}, []
    plan = allocate_timeline("\n".join(f"{i*8}-{(i+1)*8}秒：动作{i}" for i in range(5)), [8])

    class Runtime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 2:
                raise RuntimeError("provider offline")
            return SimpleNamespace(final_response=json.dumps({"shots":
                [shot(i) for i in range(1, 5)] if len(calls) == 1 else [shot(5)]}), manifest={})

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match="provider offline"):
        asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state=state, save=noop, progress=noop, batch_attempts=1))
    assert len(state["valid"]) == 4
    text, _ = asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state=state, save=noop, progress=noop))
    assert len(calls) == 3
    assert len(json.loads(text)["shots"]) == 5


def test_transient_failed_batch_retries_automatically_without_repeating_first():
    calls, state = [], {}
    plan = allocate_timeline("0-40秒：完整连续动作", [8])

    class Runtime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 2:
                raise RuntimeError("连接中断")
            rows = [shot(i) for i in range(1, 5)] if len(calls) == 1 else [shot(5)]
            return SimpleNamespace(final_response=json.dumps({"shots": rows}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state=state,
                                    save=noop, progress=noop))
    assert len(calls) == 3 and len(json.loads(result)["shots"]) == 5
    assert not state["failed_units"]


def test_planned_truncated_reply_keeps_complete_rows_and_only_requests_missing_slot():
    plan = allocate_timeline("0-8秒：开场\n8-16秒：反击", [8])
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            text = '{"shots":[' + json.dumps(shot(1)) + ', {"title": "未完' if len(calls) == 1 else json.dumps({"shots": [shot(2)]})
            return SimpleNamespace(final_response=text, manifest={})

    async def noop(*args):
        pass

    text, _ = asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state={}, save=noop, progress=noop))
    assert len(calls) == 2
    assert "仅返回第2镜" in calls[1].prompt
    assert json.loads(text)["shots"][0]["title"] == "镜头1"


def test_multiple_short_shots_share_one_video_and_survive_schema_and_prompt():
    from app.domain.schemas import StoryboardShotCreate
    from app.services.clip_timeline import video_timeline_instruction
    from app.services.source_timeline import validate_shots
    from app.services.task_worker import StoryboardGenerationPayload
    source = "0-2秒：女子拔剑\n2-5秒：切近景连续交锋\n5-15秒：侧面跟拍巨龙俯冲"
    plan = allocate_timeline(source, list(range(4, 16)))

    class Runtime:
        async def run(self, req):
            assert "internal_shots" in req.prompt
            assert "内部镜头可短至1–2秒" in req.system_prompt
            return SimpleNamespace(final_response=json.dumps({"shots": [shot(1, 15)]}), manifest={})

    async def noop(*args):
        pass

    text, _ = asyncio.run(generate(request(), Runtime, plan=plan, durations=list(range(4, 16)),
                                  state={}, save=noop, progress=noop))
    board = StoryboardGenerationPayload.model_validate_json(text)
    validate_shots(source, board.shots)
    assert len(board.shots) == 1
    assert len(board.shots[0].internal_shots) == 3
    payload = StoryboardShotCreate(**board.shots[0].model_dump(exclude={'asset_names'}))
    instruction = video_timeline_instruction(payload.model_dump(mode='json')['internal_shots'])
    assert '0–2秒' in instruction and '2–5秒' in instruction and '5–15秒' in instruction
    assert '连续交锋' in instruction
    assert '不是分别提交的视频任务' in instruction


def test_internal_absolute_times_remain_correct_across_video_files():
    from app.services.source_timeline import validate_shots
    from app.services.task_worker import GeneratedStoryboardShotPayload
    source = '0-2秒：拔剑\n2-9秒：进攻\n9-15秒：反击'
    plan = allocate_timeline(source, list(range(4, 13)))
    board = [GeneratedStoryboardShotPayload(title='片段', image_prompt='首帧',
        duration_seconds=p['duration_seconds'], internal_shots=p['internal_shots']) for p in plan]
    validate_shots(source, board)
    assert len(board) == 2
    assert plan[1]['internal_shots'][0]['start_seconds'] == 0
    assert plan[1]['internal_shots'][0]['source_start_seconds'] == plan[1]['start_seconds']
    board[1].internal_shots[0].source_start_seconds = 0
    with pytest.raises(ValueError, match='绝对时间不一致'):
        validate_shots(source, board)


# The chapter that exposed the bug: an imported novel with scene markers and no
# timestamps at all. A single request for the whole chapter returned two shots
# covering the opening scene, and the board looked complete.
PROSE_CHAPTER = """场1｜内景｜出租屋｜黄昏
人物：苏郁
△ 苏郁合上笔记本，把打火机塞进裤兜出门。
苏郁（低声）：放屁。
---
场2｜外景｜拆迁区小路｜夜
人物：苏郁
△ 疯狗扑上来咬住他的左小腿，他抡起砖石砸下去。
苏郁（喘气）：这狗……不对劲。
---
场3｜内景｜医院走廊｜夜
人物：梁雪、护士长
护士长：梁雪，你这状态不能再上了，回去睡。
"""


def prose_runtime(shot_titles):
    """A runtime that returns exactly the shots it is asked to produce."""
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            number = len(calls)
            return SimpleNamespace(final_response=json.dumps({"shots": [
                {**shot(1), "title": shot_titles[number - 1], "order_index": 1}]}), manifest={})

    return Runtime, calls


def test_prose_chapter_is_split_by_its_scene_markers():
    segments = segment_script(PROSE_CHAPTER)
    assert [s["label"] for s in segments] == [
        "场1｜内景｜出租屋｜黄昏",
        "场2｜外景｜拆迁区小路｜夜",
        "场3｜内景｜医院走廊｜夜",
    ]
    # The scene separator is structural, not part of any scene's prose.
    assert all("---" not in s["content"] for s in segments)


def test_an_unsegmented_block_without_scene_markers_still_produces_a_unit():
    segments = segment_script("苏郁推开窗，街上没有人。")
    assert len(segments) == 1
    assert segments[0]["content"] == "苏郁推开窗，街上没有人。"
    assert segment_script("") == []


def test_markerless_prose_long_enough_to_truncate_is_chunked():
    # The imported chapter had no scene markers, so one request answered a
    # 4000-character chapter with only its opening shots.
    chapter = "\n".join(f"第{i}段：苏郁在闷热的房间里翻看视频，记者围住发布会现场。" * 8
                      for i in range(1, 40))
    segments = segment_script(chapter)
    assert len(segments) > 1
    assert all(len(segment["content"]) <= 2000 for segment in segments)
    # The chunks keep the chapter's order and cover it without dropping prose.
    joined = "".join(segment["content"] for segment in segments)
    for index in range(1, 40):
        assert f"第{index}段：" in joined


def test_an_explicit_budget_overrides_the_markerless_default():
    chapter = "\n".join(f"第{i}段：正文内容。" for i in range(1, 60))
    segments = segment_script(chapter, budget=100)
    assert len(segments) > 1
    assert all(len(segment["content"]) <= 100 for segment in segments)


def test_a_single_paragraph_longer_than_the_budget_is_hard_wrapped():
    # Some imported chapters are one giant paragraph; dropping to the
    # paragraph-splitting path alone would leave it whole and truncate again.
    chapter = "苏郁" + "很热很闷的夏天里他翻着视频。" * 500
    segments = segment_script(chapter, budget=300)
    assert len(segments) > 1
    assert all(len(segment["content"]) <= 300 for segment in segments)
    assert "".join(segment["content"] for segment in segments) == chapter


def test_an_oversized_scene_is_split_into_parts():
    chapter = "场1｜内景｜出租屋｜黄昏\n" + "\n".join(f"△ 第{i}段落。" * 20 for i in range(40))
    segments = segment_script(chapter, budget=400)
    assert len(segments) > 1
    assert all(len(s["content"]) <= 400 for s in segments)
    assert [s["part"] for s in segments] == list(range(1, len(segments) + 1))
    assert all(s["parts"] == len(segments) for s in segments)


def test_every_prose_scene_is_generated_and_shots_are_numbered_across_them():
    runtime, calls = prose_runtime(["开场", "遇袭", "医院"])

    async def noop(*args):
        pass

    text, _ = asyncio.run(generate(request(), runtime, plan=[], durations=[8], state={},
                                   save=noop, progress=noop, script=PROSE_CHAPTER))
    shots = json.loads(text)["shots"]
    # One request per scene, and every scene actually reached the board.
    assert len(calls) == 3
    assert [s["title"] for s in shots] == ["开场", "遇袭", "医院"]
    # Every request numbered its shot 1, because each scene is generated blind
    # to the others. The platform has to renumber them: a scene-local 1 that was
    # kept would collide with the previous scene and drop shots from the board.
    assert len(shots) == 3
    # Each request is scoped to its own scene, and the separator never leaks in.
    assert "本场正文" in calls[0].prompt and "疯狗" not in calls[0].prompt
    assert "疯狗" in calls[1].prompt and "护士长" not in calls[1].prompt
    assert "第 2/3 个场景" in calls[1].prompt


def test_a_scene_that_produces_no_shots_fails_instead_of_publishing_a_partial_board():
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            if len(calls) >= 2:
                # The second scene answers with no shots, and its format repair
                # does not recover any either.
                return SimpleNamespace(final_response=json.dumps({"shots": []}), manifest={})
            return SimpleNamespace(final_response=json.dumps({"shots": [shot(1)]}), manifest={})

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match="分镜"):
        asyncio.run(generate(request(), runtime_factory=Runtime, plan=[], durations=[8],
                             state={}, save=noop, progress=noop, script=PROSE_CHAPTER))


def test_completed_scenes_are_not_regenerated_on_a_resume():
    state = {}
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            if len(calls) == 2:
                raise RuntimeError("provider offline")
            return SimpleNamespace(final_response=json.dumps({"shots": [shot(1)]}), manifest={})

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match="provider offline"):
        asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
                             save=noop, progress=noop, script=PROSE_CHAPTER, batch_attempts=1))
    assert len(calls) == 2
    # The first scene is checkpointed; resuming must not pay for it again.
    assert state["segment_shots"].get("1")

    runtime2, calls2 = prose_runtime(["开场", "遇袭", "医院"])
    text, _ = asyncio.run(generate(request(), runtime2, plan=[], durations=[8], state=state,
                                   save=noop, progress=noop, script=PROSE_CHAPTER))
    assert len(calls2) == 2
    assert len(json.loads(text)["shots"]) == 3


def test_each_scene_gets_a_share_of_the_chapter_budget_not_the_whole_thing():
    # Giving every scene the full chapter budget would multiply the runtime by
    # the number of scenes, so the share is proportional to the scene's length.
    runtime, calls = prose_runtime(["开场", "遇袭", "医院"])

    async def noop(*args):
        pass

    asyncio.run(generate(request(), runtime, plan=[], durations=[8], state={}, save=noop,
                         progress=noop, script=PROSE_CHAPTER, budget=90))
    assert len(calls) == 3
    shares = []
    for call_request in calls:
        match = re.search(r"本场的目标成片时长约 (\d+) 秒", call_request.prompt)
        assert match, call_request.prompt[-400:]
        shares.append(int(match.group(1)))
    assert len(set(shares)) > 1
    # Every share is a fraction of the chapter, never the chapter total.
    assert all(0 < share < 90 for share in shares)
    assert sum(shares) <= 90 + len(shares)
    # The longest scene carries the largest share, no matter where it sits in
    # the chapter.
    lengths = [len(segment["content"]) for segment in segment_script(PROSE_CHAPTER)]
    assert shares[lengths.index(max(lengths))] == max(shares)


def test_duration_overrun_retries_only_the_batch_that_exceeded_budget():
    calls, state = [], {}
    script = "场1｜开场\n进门\n场2｜结尾\n离开"

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            rows = [shot(1)] if len(calls) != 2 else [shot(1), shot(2), shot(3)]
            return SimpleNamespace(final_response=json.dumps({"shots": rows}), manifest={})

    async def noop(*args):
        pass

    output, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
        save=noop, progress=noop, script=script, budget=20))
    assert len(calls) == 3 and len(json.loads(output)["shots"]) == 2
    assert "本批总时长超过" in calls[-1]
    assert sum("本场正文：\n场1" in p for p in calls) == 1


def test_segment_offset_counts_the_shots_of_the_preceding_scenes():
    segments = segment_script(PROSE_CHAPTER)
    state = {"segment_shots": {"1": ["1", "2"], "2": ["3"]}}
    assert segment_offset(segments, state, segments[0]) == 0
    assert segment_offset(segments, state, segments[1]) == 2
    assert segment_offset(segments, state, segments[2]) == 3


def test_existing_shots_are_traced_to_the_scene_they_were_written_from():
    segments = segment_script(PROSE_CHAPTER)
    stored = [
        {**shot(1), "dialogue": "放屁。", "title": "合上笔记本"},
        {**shot(2), "dialogue": "这狗……不对劲。", "title": "疯狗扑咬"},
    ]
    matched = match_existing_shots(segments, stored)
    assert [row["dialogue"] for row in matched["1"]] == ["放屁。"]
    assert [row["dialogue"] for row in matched["2"]] == ["这狗……不对劲。"]
    # The third scene was never storyboarded, which is the coverage gap the
    # repair has to notice instead of passing off as a complete board.
    assert matched["3"] == []


def test_coverage_finding_flags_a_board_with_fewer_shots_than_scenes():
    segments = segment_script(PROSE_CHAPTER)
    assert coverage_finding([shot(1), shot(2), shot(3)], segments) is None
    gap = coverage_finding([shot(1)], segments)
    assert gap is not None and gap["severity"] == "blocking"
    # A chapter without scene markers is a single scene, so one shot covers it.
    assert coverage_finding([shot(1)], segment_script("苏郁推门出去。")) is None


def test_coverage_ignores_timed_chapters_whose_clips_hold_several_cuts():
    # A source with a complete timeline is packed into long clips, so a board
    # with fewer shots than scenes is normal there, not a coverage gap.
    timed = "0-8秒：开场\n8-16秒：反击"
    assert scenes_for_coverage(timed, [8]) == []
    assert scenes_for_coverage(timed, [4, 8, 12]) == []
    # The same condition as generate(): an untimed chapter is scene-segmented.
    assert scenes_for_coverage(PROSE_CHAPTER, [8]) == segment_script(PROSE_CHAPTER)
    # A timeline the model cannot express is a timing error, not a coverage gap.
    assert scenes_for_coverage(timed, []) == []


def test_repair_keeps_every_scene_and_never_publishes_a_shorter_board():
    calls = []
    stored = [
        {**shot(1), "dialogue": "放屁。", "title": "开场"},
        {**shot(2), "dialogue": "这狗……不对劲。", "title": "遇袭"},
    ]
    replies = [
        {"shots": [{**shot(1), "dialogue": "放屁。", "title": "开场"}]},
        {"shots": [{**shot(1), "dialogue": "这狗……不对劲。", "title": "遇袭"}]},
        {"shots": [{**shot(1), "title": "医院"}]},
    ]

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(final_response=json.dumps(replies[len(calls) - 1]), manifest={})

    async def noop(*args):
        pass

    text, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={},
                                   save=noop, progress=noop, script=PROSE_CHAPTER,
                                   repair_shots=stored))
    shots = json.loads(text)["shots"]
    # Repair still visits every scene, including the one the stored board was
    # missing, instead of rewriting the whole chapter in a single request.
    assert len(calls) == 3
    assert len(shots) == 3
    assert "本场正文" in calls[2].prompt
    assert shots[2]["title"] == "医院"


def test_repair_that_drops_shots_fails_instead_of_replacing_the_board():
    stored = [{**shot(1), "dialogue": "放屁。", "title": "开场"}]

    class Runtime:
        async def run(self, req):
            # The model answers every scene with an empty board.
            return SimpleNamespace(final_response=json.dumps({"shots": []}), manifest={})

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match="未采用|未覆盖|修复"):
        asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={},
                             save=noop, progress=noop, script=PROSE_CHAPTER,
                             repair_shots=stored))


def test_a_repair_that_truncates_a_scene_clears_it_so_the_retry_can_refill_it():
    """The truncated scene must not stay checkpointed at its shorter length.

    Repair is scene-batched. If a scene comes back with fewer shots than it had,
    the guard rejects the whole repair; keeping that shorter scene in the
    checkpoint made the next attempt compare the same short board against the
    same floor and fail again in seconds.
    """
    nurse = "梁雪，你这状态不能再上了，回去睡。"
    stored = [
        {**shot(1), "dialogue": "放屁。", "title": "开场"},
        {**shot(2), "dialogue": "这狗……不对劲。", "title": "遇袭"},
        {**shot(3), "dialogue": nurse, "title": "医院-1"},
        {**shot(4), "dialogue": nurse, "title": "医院-2"},
        {**shot(5), "dialogue": nurse, "title": "医院-3"},
    ]
    state = {}
    calls = []
    replies = [
        # Scene one and two keep their shots; scene three collapses 3 into 1.
        {"shots": [{**shot(1), "dialogue": "放屁。", "title": "开场"}]},
        {"shots": [{**shot(1), "dialogue": "这狗……不对劲。", "title": "遇袭"}]},
        {"shots": [{**shot(1), "dialogue": nurse, "title": "医院-1"}]},
    ]

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(
                final_response=json.dumps(replies[min(len(calls) - 1, len(replies) - 1)]), manifest={}
            )

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match="降到"):
        asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
                             save=noop, progress=noop, script=PROSE_CHAPTER,
                             repair_shots=stored))
    # The truncated scene is gone from the checkpoint, and so is its short
    # result, so a resume re-asks for it instead of replaying the same failure.
    assert not state["segment_shots"].get("3")
    assert "3" not in state["valid"]
    # The scenes before it were kept: only the damaged tail is regenerated.
    assert state["valid"]["1"] and state["valid"]["2"]


def test_short_batch_retries_in_place_and_reindexes_later_saved_scenes():
    stored = [
        {**shot(1), "dialogue": "放屁。", "title": "开场1"},
        {**shot(2), "dialogue": "放屁。", "title": "开场2"},
        {**shot(3), "dialogue": "这狗……不对劲。", "title": "遇袭"},
        {**shot(4), "dialogue": "梁雪，你这状态不能再上了，回去睡。", "title": "医院"},
    ]
    # A legacy checkpoint contains one short scene and two valid later scenes.
    state = {"valid": {"1": {**stored[0], "order_index": 1},
                       "2": {**stored[2], "order_index": 2},
                       "3": {**stored[3], "order_index": 3}},
             "segment_shots": {"1": ["1"], "2": ["2"], "3": ["3"]}}
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(final_response=json.dumps({"shots": stored[:1] if len(calls) == 1 else stored[:2]}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
        save=noop, progress=noop, script=PROSE_CHAPTER, repair_shots=stored))
    assert len(calls) == 2
    assert [r["title"] for r in json.loads(result)["shots"]] == [r["title"] for r in stored]
    assert state["segment_shots"]["2"] == ["3"] and state["segment_shots"]["3"] == ["4"]


def test_partial_patch_resume_skips_already_repaired_targets():
    stored = [shot(i) for i in range(1, 4)]
    state, calls = {}, []

    class Runtime:
        fail = True

        async def run(self, req):
            calls.append(req.prompt)
            if "只修复第 3 镜" in req.prompt and self.fail:
                raise RuntimeError("断开")
            return SimpleNamespace(final_response='{"fields":{"dialogue":"新台词"}}', manifest={})

    runtime = Runtime()

    async def noop(*args):
        pass

    kwargs = dict(plan=[], durations=[8], state=state, save=noop, progress=noop,
                  repair_shots=stored, findings=[{"shot_indices": [2, 3], "severity": "major",
                                                  "fields": ["dialogue"], "issue": "台词错"}])
    with pytest.raises(RuntimeError, match="第 3 镜"):
        asyncio.run(generate(request(), lambda: runtime, **kwargs))
    assert state["repaired_indices"] == [2]
    runtime.fail = False
    result, _ = asyncio.run(generate(request(), lambda: runtime, **kwargs))
    assert sum("只修复第 2 镜" in p for p in calls) == 1
    assert json.loads(result)["shots"][0]["dialogue"] == stored[0]["dialogue"]


def test_invalid_shot_retry_budget_is_not_multiplied_by_batch_retries():
    plan = allocate_timeline("0-24秒：连续动作", [8])
    state, calls = {}, []

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            if len(calls) == 1:
                text = json.dumps({"shots": [shot(1), {**shot(2), "image_prompt": ""}, shot(3)]})
            else:
                raise RuntimeError("供应商断开")
            return SimpleNamespace(final_response=text, manifest={})

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match="第2镜修复失败"):
        asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state=state,
                             save=noop, progress=noop))
    assert len(calls) == 4  # One generation plus three repairs, not 3 * 3 repairs.
    assert set(state["valid"]) == {"1", "3"}


def test_schema_repair_cannot_overwrite_good_dialogue_or_action():
    calls = []
    original = {**shot(1), "image_prompt": ""}

    class Runtime:
        async def run(self, req):
            calls.append(req)
            row = original if len(calls) == 1 else {
                **shot(1), "image_prompt": "修好的首帧", "dialogue": "不该改的台词", "action_description": "不该改的动作"}
            return SimpleNamespace(final_response=json.dumps({"shots": [row]}), manifest={})

    async def noop(*args):
        pass

    text, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state={},
                                  save=noop, progress=noop))
    row = json.loads(text)["shots"][0]
    assert row["image_prompt"] == "修好的首帧"
    assert row["dialogue"] == original["dialogue"]
    assert row["action_description"] == original["action_description"]
    assert len(calls) == 2


def test_resume_uses_latest_partial_candidate_and_keeps_later_valid_shots():
    state, calls = {}, []
    plan = allocate_timeline("0-24秒：连续动作", [8])

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            if len(calls) == 1:
                rows = [shot(1), {**shot(2), "image_prompt": "", "title": ""}, shot(3)]
                text = json.dumps({"shots": rows})
            elif len(calls) == 2:
                text = '{"fields":{"title":"已修好的标题"}}'
            else:
                assert '"title": "已修好的标题"' in req.prompt
                text = '{"fields":{"image_prompt":"已修好的首帧"}}'
            return SimpleNamespace(final_response=text, manifest={})

    async def noop(*args):
        pass

    kwargs = dict(plan=plan, durations=[8], state=state, save=noop, progress=noop, batch_attempts=1)
    with pytest.raises(RuntimeError, match="第2镜修复失败"):
        asyncio.run(generate(request(), Runtime, **kwargs))
    state = copy.deepcopy(state)  # Simulate a worker restart from persisted JSON.
    kwargs["state"] = state
    text, _ = asyncio.run(generate(request(), Runtime, **kwargs))
    assert len(calls) == 3
    rows = json.loads(text)["shots"]
    assert rows[1]["title"] == "已修好的标题"
    assert rows[1]["image_prompt"] == "已修好的首帧"
    assert [rows[i]["title"] for i in (0, 2)] == ["镜头1", "镜头3"]


def test_targeted_patch_builds_on_saved_partial_fields_after_restart():
    stored = [{**shot(1), "title": "", "image_prompt": ""}, shot(2)]
    state, calls = {}, []

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            if len(calls) == 1:
                text = '{"fields":{"title":"局部标题"}}'
            else:
                assert '"title": "局部标题"' in req.prompt
                text = '{"fields":{"image_prompt":"局部首帧"}}'
            return SimpleNamespace(final_response=text, manifest={})

    async def noop(*args):
        pass

    kwargs = dict(plan=[], durations=[8], state=state, save=noop, progress=noop, batch_attempts=1,
                  repair_shots=stored, findings=[{"shot_indices": [1], "severity": "major",
                    "fields": ["title", "image_prompt"], "issue": "缺少标题和首帧"}])
    with pytest.raises(RuntimeError, match="第 1 镜局部修复"):
        asyncio.run(generate(request(), Runtime, **kwargs))
    kwargs["state"] = copy.deepcopy(state)
    text, _ = asyncio.run(generate(request(), Runtime, **kwargs))
    assert len(calls) == 2
    rows = json.loads(text)["shots"]
    assert rows[0]["title"] == "局部标题" and rows[0]["image_prompt"] == "局部首帧"
    assert rows[1]["dialogue"] == stored[1]["dialogue"]


def test_legacy_cached_batch_repairs_bad_timeline_without_regenerating_scene():
    invalid = {**shot(1), "emotion_plan": {"beats": [{"start": 0, "end": 12,
        "character": "女主", "emotion": "平静克制"}]}}
    state = {"valid": {"1": invalid, "2": shot(2)}, "segment_shots": {"1": ["1", "2"]}}
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            assert "仅返回第1镜" in req.prompt
            return SimpleNamespace(final_response=json.dumps({"fields": {"emotion_plan": {
                "beats": [{"start": 0, "end": 8, "character": "女主", "emotion": "平静克制"}]}}}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(request(), Runtime, plan=[], durations=[8], state=state,
        save=noop, progress=noop, script="场1｜雨夜\n连续对话"))
    assert len(calls) == 1
    rows = json.loads(result)["shots"]
    assert rows[0]["emotion_plan"]["beats"][0]["end"] == 8
    assert rows[1]["title"] == shot(2)["title"]


def test_legacy_timed_checkpoint_repairs_invalid_completed_shot():
    invalid = {**shot(1), "emotion_plan": {"beats": [{"start": 0, "end": 12,
        "character": "女主", "emotion": "平静克制"}]}}
    state = {"valid": {"1": invalid, "2": shot(2)}}
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req.prompt)
            assert "仅返回第1镜" in req.prompt
            return SimpleNamespace(final_response=json.dumps({"fields": {"emotion_plan": {
                "beats": [{"start": 0, "end": 8, "character": "女主", "emotion": "平静克制"}]}}}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(request(), Runtime, plan=allocate_timeline("0-16秒：连续对话", [8]),
                                    durations=[8], state=state, save=noop, progress=noop))
    assert len(calls) == 1
    assert json.loads(result)["shots"][0]["emotion_plan"]["beats"][0]["end"] == 8


def test_finding_locations_name_only_the_shots_meant():
    # The emphasis form is the common board-wide one: it names a range but only
    # means the shots after the marker.
    findings = [
        {"severity": "major", "location": "镜头 01-33（重点 09→10、18→19、30→31）",
         "issue": "承接与分组自相矛盾"},
        {"severity": "major", "location": "镜头 20-30", "issue": "缺少 frame_layout"},
        {"severity": "minor", "location": "镜头 08、09、17", "issue": "台词语速偏紧"},
        {"severity": "minor", "location": "整批镜头", "issue": "多数镜头取最短档"},
    ]
    targets = parse_finding_targets(findings, 33)
    assert sorted(targets) == [8, 9, 10, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31]
    # The board-wide pacing note names no shot, so it never widens into a rewrite.
    assert all(f["location"] != "整批镜头" for items in targets.values() for f in items)


def test_finding_locations_are_clamped_to_existing_shots():
    findings = [{"severity": "major", "location": "镜500", "issue": "不存在"}]
    assert parse_finding_targets(findings, 12) == {}


def test_user_feedback_only_names_shots_when_marked_as_shots():
    # A bare number in prose ("等 3 秒") must not be read as shot three.
    assert parse_user_targets("把第3镜的动作加快", 12) == {3}
    assert parse_user_targets("镜头 5-7 重新配一下台词", 12) == {5, 6, 7}
    assert parse_user_targets("再等 3 秒后切镜", 12) == set()
    assert parse_user_targets("整版重做", 12) == set()


def test_finding_fields_reads_what_is_actually_being_asked_for():
    assert "frame_layout" in finding_fields([{"issue": "缺少 frame_layout", "suggestion": "补齐"}])
    assert "dialogue" in finding_fields([{"issue": "台词语速偏紧", "suggestion": "放慢"}])
    assert finding_fields([{"issue": "整体节奏偏慢", "suggestion": "收紧"}]) == []
    assert finding_fields([{"fields": ["frame_layout"],
        "suggestion": "统一frame_layout，同步修改image_prompt中的同一机位表述，保持dialogue不变"}]) == ["frame_layout", "image_prompt"]


def test_a_partial_patch_only_overwrites_the_named_fields():
    stored = {**shot(12), "dialogue": "原台词", "title": "原标题", "image_prompt": "原首帧"}
    rows = decode_repair_reply(json.dumps({"fields": {"dialogue": "新台词", "title": "被误改"}}))
    merged = merge_shot_patch(stored, rows, 12, ["dialogue"])
    assert merged["dialogue"] == "新台词"
    # A field the review did not name is not allowed to drift.
    assert merged["title"] == "原标题"
    assert merged["image_prompt"] == "原首帧"
    assert merged["order_index"] == 12


def test_a_full_shot_reply_still_only_contributes_the_named_fields():
    stored = {**shot(5), "dialogue": "原台词", "image_prompt": "原首帧", "title": "原标题"}
    rows = [{"dialogue": "新台词", "image_prompt": "顺手重写的首帧", "title": "新标题"}]
    merged = merge_shot_patch(stored, rows, 5, ["dialogue"])
    assert merged["dialogue"] == "新台词"
    assert merged["image_prompt"] == "原首帧"


def test_targeted_repair_touches_only_the_flagged_shot():
    stored = [
        {**shot(1), "dialogue": "放屁。", "title": "开场"},
        {**shot(2), "dialogue": "这狗……不对劲。", "title": "遇袭"},
        {**shot(3), "dialogue": "梁雪，你这状态不能再上了，回去睡。", "title": "医院"},
    ]
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(
                final_response=json.dumps({"fields": {"dialogue": "医院-改后台词"}}), manifest={}
            )

    async def noop(*args):
        pass

    text, _ = asyncio.run(generate(
        request(), Runtime, plan=[], durations=[8], state={}, save=noop, progress=noop,
        script=PROSE_CHAPTER, repair_shots=stored,
        findings=[{"severity": "minor", "location": "镜3", "issue": "台词语速偏紧", "suggestion": "放慢"}],
    ))
    # Only the flagged shot was asked about -- not the whole three-scene board.
    assert len(calls) == 1
    assert "只修复第 3 镜" in calls[0].prompt
    shots = json.loads(text)["shots"]
    assert [row["title"] for row in shots] == ["开场", "遇袭", "医院"]
    assert shots[2]["dialogue"] == "医院-改后台词"
    # The untouched shots keep their original wording exactly.
    assert shots[0]["dialogue"] == "放屁。"
    assert shots[1]["dialogue"] == "这狗……不对劲。"


def test_a_local_blocking_finding_repairs_only_the_named_shot():
    stored = [
        {**shot(1), "dialogue": "放屁。", "title": "开场"},
        {**shot(2), "dialogue": "这狗……不对劲。", "title": "遇袭"},
        {**shot(3), "dialogue": "梁雪，你这状态不能再上了，回去睡。", "title": "医院"},
    ]
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(final_response=json.dumps(
                {"fields": {"dialogue": "修复台词", "title": "不应被采用"}}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(
        request(), Runtime, plan=[], durations=[8], state={}, save=noop, progress=noop,
        script=PROSE_CHAPTER, repair_shots=stored,
        findings=[{"severity": "blocking", "location": "镜2", "issue": "台词错误",
                   "fields": ["dialogue"], "suggestion": "修复本镜台词"}],
    ))
    assert len(calls) == 1 and "只修复第 2 镜" in calls[0].prompt
    rows = json.loads(result)["shots"]
    assert [r["title"] for r in rows] == [r["title"] for r in stored]
    assert [r["dialogue"] for r in rows] == [stored[0]["dialogue"], "修复台词", stored[2]["dialogue"]]


def test_many_targets_still_preserve_every_untargeted_field():
    source = "\n".join(f"{start}-{start + 4}秒：第{start // 4 + 1}段交锋" for start in range(0, 40, 4))
    plan = allocate_timeline(source, [4])
    stored = [{**shot(index, 4), "dialogue": f"第{index}句"} for index in range(1, 11)]
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(final_response=json.dumps(
                {"fields": {"dialogue": "修复后", "image_prompt": "不该命中"}}), manifest={})

    async def noop(*args):
        pass

    result, _ = asyncio.run(generate(
        request(), Runtime, plan=plan, durations=[4], state={}, save=noop, progress=noop,
        repair_shots=stored,
        findings=[{"severity": "major", "location": "镜头 1-10", "issue": "台词节奏偏慢",
                   "suggestion": "整体缩短"}],
    ))
    assert len(calls) == 10
    assert all("只修复第" in req.prompt for req in calls)
    assert all(row["dialogue"] == "修复后" and row["image_prompt"] == stored[0]["image_prompt"]
               for row in json.loads(result)["shots"])


def test_a_scene_request_drops_the_whole_chapter_body():
    # The pipeline appends the entire script as "剧本正文：". If a scene-by-scene
    # chapter kept that body, the model would answer for every scene at once and
    # the board would be published holding only the opening shots.
    full = ("章节：第一章\n剧本：v5《末日食金者》\n资产清单：[]\n\n剧本正文：\n" + PROSE_CHAPTER)
    req = request().model_copy(update={"prompt": full})
    runtime, calls = prose_runtime(["开场", "遇袭", "医院"])

    async def noop(*args):
        pass

    asyncio.run(generate(req, runtime, plan=[], durations=[8], state={}, save=noop,
                         progress=noop, script=PROSE_CHAPTER))
    assert len(calls) == 3
    # Each request carries only the scene it is generating, never the chapter.
    assert "剧本正文" not in calls[0].prompt
    assert "疯狗" not in calls[0].prompt and "护士长" not in calls[0].prompt
    assert "疯狗" in calls[1].prompt and "护士长" not in calls[1].prompt
    assert "护士长" in calls[2].prompt and "疯狗" not in calls[2].prompt
    # The chapter metadata that is not the body still reaches every request.
    assert "末日食金者" in calls[0].prompt and "末日食金者" in calls[2].prompt


def test_a_timed_batch_only_carries_its_own_window_of_stored_shots():
    # A long timed board is hundreds of kilobytes. Sending the whole board with
    # every batch is what exhausted the context, so each request must carry
    # only its own window (plus the previous shot for continuity).
    plan = allocate_timeline("\n".join(f"{i * 8}-{(i + 1) * 8}秒：动作{i}" for i in range(12)), [8])
    stored = [{**shot(i), "title": f"UNIQUETITLE{i}", "scene_description": f"UNIQUESCENE{i}"}
              for i in range(1, 13)]
    prompts = []

    class Runtime:
        async def run(self, req):
            prompts.append(req.prompt)
            first = len(prompts) * 4 - 3
            return SimpleNamespace(
                final_response=json.dumps({"shots": [shot(i) for i in range(first, first + 4)]}),
                manifest={},
            )

    async def noop(*args):
        pass

    asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state={},
                         save=noop, progress=noop, repair_shots=stored))
    assert len(prompts) == 3
    def titles(text: str) -> list[int]:
        return [i for i in range(1, 13) if f'"UNIQUETITLE{i}",' in text or f'"UNIQUETITLE{i}"}}' in text]

    assert titles(prompts[0]) == [1, 2, 3, 4]
    # Later windows never re-send the whole board, only their slice.
    assert 1 not in titles(prompts[2]) and 12 in titles(prompts[2])
    assert max(len(text) for text in prompts) < 8000


def test_a_batch_that_cannot_be_repaired_is_re_asked_instead_of_replayed():
    """A failed batch must not be replayed from its own broken checkpoint.

    The live failure: one scene answered with unparseable text, that text was
    checkpointed as the batch reply, and every retry decoded the same bytes and
    failed within seconds. The run burned its whole attempt budget without ever
    asking the model again, so the chapter could only be rescued by hand.
    """
    state, saves = {}, []
    first_run_calls = []

    class BrokenRuntime:
        async def run(self, req):
            first_run_calls.append(req)
            if len(first_run_calls) == 1:
                return SimpleNamespace(final_response=json.dumps({"shots": [shot(1)]}), manifest={})
            # Neither the scene nor its format repair produces usable JSON.
            return SimpleNamespace(final_response="已保存分镜文件，但没有给出 JSON。", manifest={})

    async def save(value):
        saves.append(copy.deepcopy(value))

    async def noop(*args):
        pass

    with pytest.raises(RuntimeError, match="缺少有效shots数组|截断"):
        asyncio.run(generate(request(), BrokenRuntime, plan=[], durations=[8], state=state,
                             save=save, progress=noop, script=PROSE_CHAPTER, batch_attempts=1))
    # The scene was asked twice (generation + format repair) and failed.
    assert len(first_run_calls) == 3
    # Its broken reply is not left behind to be replayed on the next attempt.
    assert "2" not in state["raw"]
    assert "2" not in state["segment_shots"]
    # Scene one is checkpointed and must be skipped on resume.
    assert state["segment_shots"]["1"] == ["1"]

    second_run_calls = []

    class WorkingRuntime:
        async def run(self, req):
            second_run_calls.append(req)
            if "本场正文" in req.prompt and "疯狗" in req.prompt:
                return SimpleNamespace(final_response=json.dumps({"shots": [shot(1)]}), manifest={})
            return SimpleNamespace(final_response=json.dumps({"shots": [shot(1)]}), manifest={})

    text, _ = asyncio.run(generate(request(), WorkingRuntime, plan=[], durations=[8], state=state,
                                   save=save, progress=noop, script=PROSE_CHAPTER))
    # Scene one was already paid for; resume starts at the failed scene.
    assert len(second_run_calls) == 2
    assert "疯狗" in second_run_calls[0].prompt
    assert "护士长" in second_run_calls[1].prompt
    assert len(json.loads(text)["shots"]) == 3
