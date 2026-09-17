from pathlib import Path

from app.services.creation_context import contains_combat


def test_combat_priority_is_scoped_and_detects_signature_techniques():
    from app.services.creation_context import combat_stage_guidance
    assert contains_combat("以神诀施展法天象地")
    assert contains_combat("连招之后借力破招")
    assert contains_combat("枪仙蓄力完毕、枪尖斜指下段瞬间切至下一镜突刺")
    assert combat_stage_guidance("video-prompt-generation", "两人安静喝茶") == ""
    rules = combat_stage_guidance("video-prompt-generation", "剑客交手")
    assert "系统战斗专项规则优先于导演手册、画风手册" in rules
    assert "不改变画风、人物外观" in rules
    assert "实际模型能力" in rules
    script = combat_stage_guidance("script-generation", "安静的日常生活")
    assert "没有则不新增打斗" in script


def test_combat_skill_is_conditionally_detected():
    assert contains_combat("女子闪避龙爪后挥剑格挡")
    assert contains_combat("the hero enters a battle")
    assert not contains_combat("两人在教室里讨论明天的课程")


def test_combat_rules_are_in_both_generation_templates():
    root = Path(__file__).parents[1] / "app/services/system_prompt_templates"
    storyboard = (root / "storyboard-generation.md").read_text(encoding="utf-8")
    video = (root / "video-prompt-generation.md").read_text(encoding="utf-8")
    for content in (storyboard, video):
        assert "战斗镜头" in content
        assert "真实尾帧" in content
        assert "攻防" in content
        assert "首帧" in content


def test_combat_templates_do_not_require_independent_first_frames():
    root = Path(__file__).parents[1] / "app/services/system_prompt_templates"
    storyboard = (root / "storyboard-generation.md").read_text(encoding="utf-8")
    video = (root / "video-prompt-generation.md").read_text(encoding="utf-8")
    assert "不安排独立镜头首帧图片任务" in storyboard
    assert "不得要求补做独立打斗首帧" in video
