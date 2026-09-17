from copy import deepcopy

import pytest

from app.services.combat_choreography import CombatDesign, CombatPlan, compile_prompt


def design_payload():
    return {
        "plan": {"windows": [{"start": 0, "end": 5, "balance": "balanced",
            "participants": ["剑仙", "枪仙"], "dominant": "", "objective": "争夺通路",
            "outcome": "双方未分胜负", "techniques": [], "constraints": "保持空间连续"}]},
        "setting": "仙宫残台，日光从西侧照入，沿用人物参考",
        "beats": [{"start": 0, "end": 2, "window": 0, "phase": "exchange",
            "actions": ["枪仙挺枪突刺，剑仙侧移压枪", "剑仙借接触翻腕回斩，枪仙撤步旋枪挡开",
                        "枪仙顺势横扫，剑仙俯身反切", "两人借兵器回弹再度变线，沿石台侧向交锋"],
            "camera": "相机同步侧向追拍交锋中心", "lighting": "主光稳定，接触火花短暂照亮武器"},
            {"start": 2, "end": 4, "window": 0, "phase": "exchange",
             "actions": ["剑仙错步压住枪杆后撩剑，枪仙转胯回抽卸力", "枪仙顺回抽点向中线，剑仙翻腕挑开后反刺"],
             "camera": "维持轴线追拍双方错步", "lighting": "沿用日光，剑尖接触闪出短促火星"},
            {"start": 4, "end": 5, "window": 0, "phase": "exchange",
             "actions": ["枪仙架杆压住来剑，剑仙俯身借杆翻刃", "剑仙顺势横斩，枪仙侧移转杆拦截"],
             "camera": "跟随两人侧移保持接触点可读", "lighting": "火星沿剑刃切向飞散，主光稳定"}],
        "end_state": "枪剑接触未分离，双方继续向石台东侧移动"}


def test_compilation_keeps_every_action_without_second_model_summary():
    design = CombatDesign.model_validate(design_payload())
    design.validate_timeline(5, design.plan)
    for protocol in ({"name": "generic"}, {"name": "minimax_h3"}):
        text = compile_prompt(design, protocol)
        for action in design.beats[0].actions:
            assert text.count(action) == 1
        assert design.beats[0].camera in text
        assert design.end_state in text


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
