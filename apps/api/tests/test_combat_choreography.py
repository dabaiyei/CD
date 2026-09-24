import json
from copy import deepcopy

import pytest

from app.services.combat_choreography import CombatDesign, CombatPlan, compile_prompt


def design_payload():
    return {
        "plan": {"windows": [{"start": 0, "end": 5, "balance": "balanced",
            "participants": ["剑仙", "枪仙"], "dominant": "", "objective": "争夺通路",
            "outcome": "双方未分胜负", "techniques": [], "constraints": "保持空间连续"}]},
        "setting": "仙宫残台，日光从西侧照入，沿用人物参考",
        "identity_lock": "剑仙保持人物资产的面容服装与长剑，枪仙保持其人物资产与银枪，不互换武器",
        "beats": [{"start": 0, "end": 2, "window": 0, "phase": "exchange",
            "actions": ["枪仙挺枪突刺，剑仙侧移压枪", "剑仙借接触翻腕回斩，枪仙撤步旋枪挡开",
                        "枪仙顺势横扫，剑仙俯身反切", "两人借兵器回弹再度变线，沿石台侧向交锋"],
            "impact": "枪剑接触向外弹开，剑仙顺回弹翻腕，枪仙撤步卸力",
            "expression": "剑仙眼神收紧盯住枪尖，眉头下压，嘴唇抿成直线，前臂绷紧，像已经看穿来势",
            "continuity": "两人侧向动量不减，压枪的剑刃接下一拍撩剑",
            "camera": "相机同步侧向追拍交锋中心", "lighting": "主光稳定，接触火花短暂照亮武器"},
            {"start": 2, "end": 4, "window": 0, "phase": "exchange",
             "actions": ["剑仙错步压住枪杆后撩剑，枪仙转胯回抽卸力", "枪仙顺回抽点向中线，剑仙翻腕挑开后反刺"],
            "impact": "枪杆震回，枪仙转胯吸收冲击后压向来剑",
            "expression": "枪仙眼神一沉，嘴角压住，下颌收紧，肩背发力，像不打算让出通路",
            "continuity": "剑仙保持反刺前倾，枪仙架杆接住来剑，脚下继续东移",
             "camera": "维持轴线追拍双方错步", "lighting": "沿用日光，剑尖接触闪出短促火星"},
            {"start": 4, "end": 5, "window": 0, "phase": "exchange",
             "actions": ["枪仙架杆压住来剑，剑仙俯身借杆翻刃", "剑仙顺势横斩，枪仙侧移转杆拦截"],
            "impact": "横斩压弯枪杆，枪仙屈膝侧移卸力，接触点短促火星熄灭",
            "expression": "剑仙眼神锐利扫向枪杆，眉毛轻挑，嘴唇微张又收住，重心压低继续追击",
            "continuity": "枪剑保持接触，双方带侧向动量进入下一片段，不重新站定",
             "camera": "跟随两人侧移保持接触点可读", "lighting": "火星沿剑刃切向飞散，主光稳定"}],
        "end_state": "枪剑接触未分离，双方继续向石台东侧移动"}


def test_compilation_keeps_every_action_without_second_model_summary():
    design = CombatDesign.model_validate(design_payload())
    design.validate_timeline(5, design.plan)
    design.validate_quality()
    for protocol in ({"name": "generic"}, {"name": "minimax_h3"}):
        text = compile_prompt(design, protocol)
        for action in design.beats[0].actions:
            assert text.count(action) == 1
        assert design.beats[0].camera in text
        assert design.end_state in text
        assert design.identity_lock in text
        for beat in design.beats:
            assert beat.impact in text
            assert beat.continuity in text


@pytest.mark.parametrize("missing", ["identity_lock", "impact", "continuity"])
def test_combat_quality_rejects_missing_identity_impact_or_continuity(missing):
    data = design_payload()
    if missing == "identity_lock":
        data[missing] = ""
    else:
        data['beats'][0][missing] = " "
    with pytest.raises(ValueError):
        CombatDesign.model_validate(data).validate_quality()


def test_still_hold_does_not_require_fabricated_impact():
    data = design_payload()
    for beat in data['beats']:
        beat.update(phase="hold", impact="")
    CombatDesign.model_validate(data).validate_quality()


def test_effect_layers_are_compiled_verbatim_without_replacing_actions():
    data = design_payload()
    data['renderer'] = 'UE5'
    effect = {'layer': 'particles', 'start': .2, 'end': .5,
        'emitter': '枪剑接触点', 'appearance': '橙白细碎火星',
        'motion': '沿剑刃切向飞溅', 'decay': '快速变暗熄灭'}
    data['beats'][0]['effects'] = [effect, {**effect, 'layer': 'volume', 'end': 3,
        'appearance': '撞击地面的灰黄烟团', 'decay': '后续节拍继续卷动沉降'}]
    design = CombatDesign.model_validate(data)
    design.validate_timeline(5)
    for protocol in ({'name': 'generic'}, {'name': 'minimax_h3', 'preferred_prompt_language': 'en'}):
        text = compile_prompt(design, protocol)
        assert 'Unreal Engine 5' in text
        for field in ('emitter', 'appearance', 'motion', 'decay'):
            assert effect[field] in text
        for beat in design.beats:
            assert all(action in text for action in beat.actions)
        assert '后续节拍继续卷动沉降' in text


@pytest.mark.parametrize('start,end', [(-1, 1), (2, 3), (.2, 6), (1, .5)])
def test_invalid_effect_timing_is_rejected(start, end):
    data = design_payload()
    data['beats'][0]['effects'] = [{'layer': 'particles', 'start': start, 'end': end,
        'emitter': '接触点', 'appearance': '火星', 'motion': '切向飞散', 'decay': '衰减'}]
    with pytest.raises(ValueError):
        CombatDesign.model_validate(data).validate_timeline(5)


def test_effect_catalog_routes_by_action_and_does_not_force_renderer():
    from app.services.combat_effects import effect_context
    melee = effect_context({'action_description': '刀剑格挡后连续斩击'})
    assert '近战：' in melee
    assert '雷电：' not in melee and '火焰：' not in melee
    spell = effect_context({'action_description': '释放雷光电弧'})
    assert '雷电：' in spell and '火焰：' not in spell
    assert '手绘2D/赛璐璐或画风不明确时留空' in spell
    text = compile_prompt(CombatDesign.model_validate(design_payload()), {'name': 'generic'})
    assert 'Unreal Engine 5' not in text and 'Octane' not in text


@pytest.mark.parametrize("mutation", ["overrun", "gap", "sparse", "unbound", "wrong_outcome"])
def test_invalid_or_rewritten_timeline_is_rejected(mutation):
    data = design_payload()
    expected = CombatPlan.model_validate(deepcopy(data["plan"]))
    if mutation == "overrun":
        data["beats"][0]["end"] = 6
    elif mutation == "gap":
        data["beats"][0]["start"] = 1
    elif mutation == "sparse":
        data["beats"][0]["actions"] = ["双方打一招"]
    elif mutation == "unbound":
        data["beats"][0]["window"] = None
    else:
        data["plan"]["windows"][0]["outcome"] = "枪仙死亡"
    with pytest.raises(ValueError):
        CombatDesign.model_validate(data).validate_timeline(5, expected)


def test_overwhelming_requires_single_decisive_strike_and_allows_spectacle():
    data = design_payload()
    data["plan"]["windows"][0].update(balance="overwhelming", dominant="剑仙", outcome="枪仙被击退")
    with pytest.raises(ValueError):
        CombatDesign.model_validate(data).validate_timeline(5)
    strike = data["beats"][0]
    data["beats"] = [strike]
    strike.update(end=.5, phase="decisive_strike", actions=["剑仙一击穿透防御，枪仙被冲击推出"])
    data["beats"].append({**strike, "start": .5, "end": 5, "phase": "spectacle",
                          "actions": ["剑光余波沿石台扩散，碎石从接触点飞散，不重复命中"]})
    CombatDesign.model_validate(data).validate_timeline(5)


def test_explicit_showdown_does_not_require_attacks():
    data = design_payload()
    data["plan"]["windows"][0].update(balance="showdown")
    data["beats"] = [data["beats"][0]]
    data["beats"][0].update(end=5, phase="hold", actions=["双方静立对视"])
    CombatDesign.model_validate(data).validate_timeline(5)


class _Reply:
    """Stand-in for AgentRuntimeResponse, which the retry loop only reads two fields from."""

    def __init__(self, final_response, finish_reason):
        self.final_response = final_response
        self.finish_reason = finish_reason
        self.manifest = {}


def _truncated_reply():
    """A reply cut off mid-JSON, exactly as a max_tokens stop produces."""
    return _Reply('{"plan": {"windows": [{"start": 0, "end": 5, "balance": "balanced", '
                  '"participants": ["剑仙", "枪仙"], "dominant": "", "objective": "争夺通路", '
                  '"outcome": "未分胜负", "constraints": "保持空间连续"}]}, "setting": "仙宫残台"',
                  "length")


@pytest.mark.parametrize("reason,expected", [
    ("length", True), ("max_tokens", True), ("max_output_tokens", True),
    ("exceed_max_iters", True), ("content_filter", True),
    ("completed", False), ("stop", False), ("", False), (None, False),
])
def test_truncation_is_read_from_the_finish_reason(reason, expected):
    from app.services.combat_choreography import truncated_output

    assert truncated_output(_Reply("{}", reason)) is expected


def test_missing_field_instruction_names_the_field_and_stays_actionable():
    from pydantic import ValidationError

    from app.services.combat_choreography import repair_instruction

    try:
        CombatDesign.model_validate(design_payload() | {"end_state": None})
    except ValidationError as exc:
        text = repair_instruction(exc, truncated=False)
    assert "end_state" in text
    assert "请不要省略" in text or "不要省略任何必填字段" in text
    # Pydantic's banner and the doc link are noise for the model being retried.
    assert "pydantic.dev" not in text
    assert "validation error for" not in text
    assert "input_value" not in text


def test_truncated_retry_asks_to_shorten_prose_not_to_drop_beats():
    from pydantic import ValidationError

    from app.services.combat_choreography import repair_instruction

    try:
        CombatDesign.model_validate(design_payload() | {"end_state": None})
    except ValidationError as exc:
        text = repair_instruction(exc, truncated=True)
    assert "截断" in text
    assert "不要减少节拍数量" in text


def test_rule_violation_is_passed_through_unchanged():
    from app.services.combat_choreography import repair_instruction

    message = "打斗模块不能修改已确认的时间轴、实力关系和结果"
    assert repair_instruction(ValueError(message), truncated=False) == message


def test_unparsable_reply_is_reported_as_truncation_when_the_model_ran_out_of_tokens():
    # The truncation branch also applies when the JSON never parsed at all.
    from app.services.combat_choreography import repair_instruction

    text = repair_instruction(RuntimeError("AI 未返回可解析的 JSON 结果"), truncated=True)
    assert "AI 未返回可解析的 JSON 结果" in text


def _complete_reply():
    return json.dumps({
        "plan": {"windows": [{"start": 0, "end": 5, "participants": ["剑仙", "枪仙"]}]},
        "setting": "仙宫残台，日光从西侧照入，沿用人物参考",
        "beats": [{"start": 0, "end": 2, "phase": "exchange",
                   "actions": ["枪仙挺枪突刺，剑仙侧移压枪"], "continuity": "承接动量"}],
        "end_state": "枪剑接触未分离，双方继续向石台东侧移动",
    }, ensure_ascii=False)


@pytest.mark.parametrize("text,expected", [
    (_complete_reply(), False),
    (_complete_reply()[:120], True),
    (_complete_reply()[:-3], True),
    ("```json\n" + _complete_reply() + "\n```", False),
    ("```json\n" + _complete_reply()[:90], True),
    ("", False),
    (json.dumps({"a": 'he said "hi"'}, ensure_ascii=False), False),
    (json.dumps({"a": "（测试）【括号】"}, ensure_ascii=False), False),
])
def test_unterminated_json_detects_a_chopped_tail(text, expected):
    from app.services.combat_choreography import unterminated_json

    assert unterminated_json(text) is expected


def test_truncation_is_detected_even_though_agentscope_reports_completed():
    # AgentScope's ReplyFinishedReason has no token-limit value: a reply stopped
    # by max_tokens is still reported as COMPLETED, so the finish reason alone
    # would never catch the real failure seen in production.
    from app.services.combat_choreography import truncated_output

    chopped = _complete_reply()[:120]
    assert truncated_output(_Reply(chopped, "completed")) is True
    assert truncated_output(_Reply(_complete_reply(), "completed")) is False
    # A provider-level signal still counts when it is present.
    assert truncated_output(_Reply(_complete_reply(), "length")) is True


def _overwhelming_design(beats, duration=8):
    plan = {"windows": [{"start": 0, "end": duration, "balance": "overwhelming",
            "participants": ["女剑仙", "应龙"], "dominant": "应龙",
            "objective": "压落岩台", "outcome": "立足点被毁", "tempo": "fast"}]}
    return CombatDesign.model_validate({
        "plan": plan, "setting": "云海孤峰", "beats": beats,
        "end_state": "她失去地面依托", "identity_lock": "按参考绑定人物外观与武器",
    }), CombatPlan.model_validate(plan)


def _beat(start, end, phase):
    return {"start": start, "end": end, "window": 0, "phase": phase,
            "actions": [f"{start:g}秒动作"], "camera": "跟拍", "lighting": "主光稳定",
            "continuity": "承接上一拍动量", "impact": "接触点受力，重心后移"}


def test_overwhelming_aftermath_shot_may_omit_the_strike():
    # A crushing blow happens once; the shot that carries its aftermath must not
    # be forced to re-enact it, so zero decisive_strike has to stay legal.
    design, plan = _overwhelming_design([_beat(0, 4, "spectacle"), _beat(4, 8, "spectacle")])
    design.validate_timeline(8, plan)


def test_overwhelming_rejects_exchange_and_names_the_beats():
    design, plan = _overwhelming_design([
        _beat(0, 2, "spectacle"), _beat(2, 6, "exchange"), _beat(6, 8, "decisive_strike")])
    with pytest.raises(ValueError) as error:
        design.validate_timeline(8, plan)
    message = str(error.value)
    assert "exchange" in message
    assert "2—6s" in message          # the offending span, not a restated rule
    assert "spectacle" in message      # tells the model what to do instead


def test_overwhelming_rejects_a_second_strike_but_allows_one():
    design, plan = _overwhelming_design([
        _beat(0, 2, "decisive_strike"), _beat(2, 6, "spectacle"), _beat(6, 8, "decisive_strike")])
    with pytest.raises(ValueError, match="decisive_strike"):
        design.validate_timeline(8, plan)

    ok, plan = _overwhelming_design([
        _beat(0, 3, "spectacle"), _beat(3, 3.8, "decisive_strike"), _beat(3.8, 8, "spectacle")])
    ok.validate_timeline(8, plan)


def test_overwhelming_strike_over_one_second_reports_the_real_span():
    design, plan = _overwhelming_design([
        _beat(0, 3, "spectacle"), _beat(3, 5, "decisive_strike"), _beat(5, 8, "spectacle")])
    with pytest.raises(ValueError) as error:
        design.validate_timeline(8, plan)
    assert "3—5s" in str(error.value)
    assert "1 秒" in str(error.value)


def test_effect_timing_error_reports_the_expected_window():
    beats = [_beat(0, 4, "spectacle"), _beat(4, 8, "spectacle")]
    beats[0]["effects"] = [{"layer": "particles", "start": 1, "end": 12,
                            "emitter": "接触点", "appearance": "火星", "motion": "切向飞散",
                            "decay": "快速熄灭"}]
    design, plan = _overwhelming_design(beats)
    with pytest.raises(ValueError) as error:
        design.validate_timeline(8, plan)
    assert "1—12s" in str(error.value)
    assert "0—4s" in str(error.value)


def test_the_request_budget_leaves_room_for_a_detailed_shot():
    # The budget only has to keep one request inside the runtime's context
    # window. A ceiling below the honest size of schema + handbooks + shot data
    # rejected valid shots, and any retry pushed them further over.
    from app.services.combat_choreography import CombatDesign, CLIP_RULES
    from app.services.martial_skill_retrieval import (
        MAX_HANDBOOK_CHARS,
        MAX_REQUEST_CHARS,
        MAX_SKILL_CHARS,
    )

    schema = len(json.dumps(CombatDesign.model_json_schema(), ensure_ascii=False))
    fixed = schema + len(CLIP_RULES) + MAX_SKILL_CHARS + MAX_HANDBOOK_CHARS
    # Comfortably more than the fixed blocks plus a realistic shot snapshot.
    assert MAX_REQUEST_CHARS > fixed * 3


def test_an_over_budget_request_shrinks_its_reference_instead_of_failing():
    from app.services.combat_choreography import _shrink_skill_reference

    schema = "SCHEMA"
    prompt = "本镜按需武指参考：\n" + ("武" * 4000) + "\n输出schema：\n" + schema + "TRAILER"
    shrunk = _shrink_skill_reference(prompt, 2500)
    assert len(shrunk) < len(prompt)
    # The schema the reply is parsed against always survives.
    assert schema in shrunk and "TRAILER" in shrunk
    assert "已截断" in shrunk


def test_shrinking_an_over_long_reference_leaves_the_schema_intact():
    from app.services.combat_choreography import _shrink_skill_reference

    prompt = "本镜按需武指参考：\n" + ("武" * 500) + "\n输出schema：\nSCHEMA"
    # Asking to remove more than the reference holds must not eat the schema.
    shrunk = _shrink_skill_reference(prompt, 99_999)
    assert "SCHEMA" in shrunk
    assert "武" * 500 not in shrunk


def test_shrinking_is_a_no_op_when_the_reference_markers_are_absent():
    from app.services.combat_choreography import _shrink_skill_reference

    assert _shrink_skill_reference("没有标记的文本", 100) == "没有标记的文本"
