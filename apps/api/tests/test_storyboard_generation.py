import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.services.agent_runtime import AgentRuntimeRequest
from app.services.storyboard_generation import allocate_timeline, complete_output, decode_shots, generate


def shot(index, duration=8):
    return {"order_index": index, "title": f"镜头{index}", "duration_seconds": duration,
            "image_prompt": "雨夜，保持人物身份", "action_description": "推门后接转身",
            "dialogue": "别走", "asset_names": []}


def request():
    return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
        prompt="剧本和模型能力", system_prompt="导演约束", model_binding={}, prompt_versions={},
        skill_versions={}, skills=[], memory_context=[])


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
        asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state=state, save=noop, progress=noop))
    assert len(state["valid"]) == 4
    text, _ = asyncio.run(generate(request(), Runtime, plan=plan, durations=[8], state=state, save=noop, progress=noop))
    assert len(calls) == 3
    assert len(json.loads(text)["shots"]) == 5


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
    from app.services.task_worker import StoryboardGenerationPayload
    from app.domain.schemas import StoryboardShotCreate
    from app.services.source_timeline import validate_shots
    from app.services.clip_timeline import video_timeline_instruction
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
    from app.services.task_worker import GeneratedStoryboardShotPayload
    from app.services.source_timeline import validate_shots
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
