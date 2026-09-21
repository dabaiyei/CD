import asyncio
import copy
import json
import re
from types import SimpleNamespace

import pytest

from app.services.agent_runtime import AgentRuntimeRequest
from app.services.storyboard_generation import (
    allocate_timeline,
    complete_output,
    decode_shots,
    generate,
    segment_offset,
    segment_script,
)


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
                             save=noop, progress=noop, script=PROSE_CHAPTER))
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


def test_segment_offset_counts_the_shots_of_the_preceding_scenes():
    segments = segment_script(PROSE_CHAPTER)
    state = {"segment_shots": {"1": ["1", "2"], "2": ["3"]}}
    assert segment_offset(segments, state, segments[0]) == 0
    assert segment_offset(segments, state, segments[1]) == 2
    assert segment_offset(segments, state, segments[2]) == 3


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
