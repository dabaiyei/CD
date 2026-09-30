import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.speech_pacing import (
    RATES, START, apply_speech_guidance, prompt_dialogue,
    speech_budget, speech_duration, speech_level, speech_lines,
)


@pytest.mark.parametrize("level", range(1, 7))
def test_six_levels_and_explicit_precedence(level):
    assert speech_level(RATES[level][2]) == level
    assert speech_level(f"语速：{level}级，低声慢语") == level
    data = speech_budget("你好" * 10, f"人声等级：{level}级")
    assert data["segments"][0]["low"] == RATES[level][0]


def test_separate_speakers_sum_their_own_rates_and_pauses():
    dialogue = "甲（语速1级，停顿2秒）：一二三四五六七八九十\n乙（语速6级）：一二三四五六七八九十"
    data = speech_budget(dialogue)
    assert data["units"] == 20
    assert [row["level"] for row in data["segments"]] == [1, 6]
    assert data["recommended_seconds"] == pytest.approx(6 + .6 + 2 + 600 / 260 + .8 + .3, abs=.01)


def test_exclude_subtitles_metadata_and_do_not_infer_from_spoken_words():
    dialogue = '【后期文字｜0—3秒】十年以后\n0–4秒 甲（轻声）：不要嘶吼。'
    assert speech_lines(dialogue) == ['不要嘶吼']
    assert speech_budget(dialogue)["units"] == 4
    assert speech_budget(dialogue)["segments"][0]["level"] == 3
    assert speech_budget("无对白")["units"] == 0


def test_walk_is_optional_and_voiceover_does_not_follow_run_speed():
    assert speech_level("全力冲刺，喊话") == 6
    assert speech_level("全力冲刺，旁白说话") == 3
    assert speech_level("语速2级，全力冲刺，喊话") == 2
    assert speech_budget("甲：你好", "语速4级")["segments"][0]["level"] == 4


def test_model_specific_duration_and_overflow():
    assert speech_duration("一二三四五六七八九十", "语速1级", [4, 8, 12], 4) == 8
    assert speech_duration("一二三四五六七八九十" * 3, "语速1级", [4, 8, 12], 4) is None
    assert speech_duration("你好", "", [5, 10], 10) == 10


def test_voice_block_refresh_and_silent_cleanup_preserve_combat_prefix():
    prompt = "ACT视角，保持原画风与动作"
    output = apply_speech_guidance(prompt, "你好", "语速1级")
    assert output.startswith("ACT视角，")
    updated = apply_speech_guidance(output, "你好", "语速4级")
    assert updated.count(START) == 1
    assert "180—200" in updated and "90—110" not in updated
    assert apply_speech_guidance(updated, "你好", audio_enabled=False) == prompt


def test_home_extracts_only_explicit_speech():
    assert prompt_dialogue('奔跑穿过街头，镜头快速后退') == ''
    assert prompt_dialogue('甲说：“快走！”乙喊：“等我！”') == '快走！\n等我！'


def test_generation_repairs_duration_locally_without_rewriting_dialogue():
    from app.services.agent_runtime import AgentRuntimeRequest
    from app.services.storyboard_generation import generate
    req = AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
        prompt="生成分镜", system_prompt="规则", model_binding={}, prompt_versions={},
        skill_versions={}, skills=[], memory_context=[])
    row = dict(order_index=1, title="对白", duration_seconds=4, image_prompt="人物正面",
        action_description="语速1级，人物站立说话", dialogue="一二三四五六七八九十", asset_names=[])
    calls = []
    class Runtime:
        async def run(self, request):
            calls.append(request.prompt)
            if len(calls) == 1:
                reply = {"shots": [row]}
            else:
                assert "语速预算不足" in request.prompt
                reply = {"fields": {"duration_seconds": 8,
                    "action_description": "0—8秒，语速1级，人物站立说话"}}
            return SimpleNamespace(final_response=json.dumps(reply), manifest={})
    async def noop(*args):
        pass
    result, _ = asyncio.run(generate(req, Runtime, plan=[], durations=[4, 8, 12],
        state={}, save=noop, progress=noop))
    output = json.loads(result)["shots"][0]
    assert len(calls) == 2
    assert float(output["duration_seconds"]) == 8
    assert output["dialogue"] == row["dialogue"]
