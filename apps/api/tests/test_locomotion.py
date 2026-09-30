import pytest

from app.services.locomotion import apply_locomotion_guidance, locomotion_guidance, MARKER


@pytest.mark.parametrize("action,level", [
    ("她悠闲行走", 1), ("她正常行走", 2), ("她快步走", 3),
    ("她小步快跑", 4), ("她快速奔跑", 5), ("她全力冲刺", 6),
])
def test_select_only_matching_gait(action, level):
    result = locomotion_guidance(action)
    assert f"{level}级" in result
    for other in range(1, 7):
        if other != level:
            assert f"{other}级" not in result


def test_mixed_people_and_speed_changes_keep_both_templates():
    result = locomotion_guidance("女孩漫步，男孩快走追上她；女孩随后全力冲刺，到终点停住")
    assert all(f"{level}级" in result for level in (1, 3, 6))
    assert "动作时间段" in result and "停步与静止要求优先" in result


@pytest.mark.parametrize("source", ["两人原地聊天", "不要奔跑，保持静止", "镜头快走", "禁止冲刺"])
def test_negative_or_camera_only_intent_does_not_add_gait(source):
    assert apply_locomotion_guidance(source) == source


def test_fixed_camera_still_allows_running_subject():
    assert "5级" in locomotion_guidance("固定摄影机，女孩快速奔跑")


def test_keeps_original_text_act_prefix_and_does_not_duplicate():
    original = "ACT视角，女子小跑后挥剑攻击"
    result = apply_locomotion_guidance(original)
    assert result.startswith("ACT视角，")
    assert result.endswith("女子小跑后挥剑攻击")
    assert result.count(MARKER) == 1
    assert apply_locomotion_guidance(result) == result


def test_running_reference_examples_do_not_turn_running_into_combat():
    from app.services.creation_context import ensure_combat_video_prefix
    enriched = apply_locomotion_guidance("女孩快速奔跑去学校")
    assert ensure_combat_video_prefix(enriched) == enriched
