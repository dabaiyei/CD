import pytest

from app.services.replica_planning import direction_context, generation_plan
from app.services.video_replica import RenderInput


def options(shots, **kwargs):
    return RenderInput(
        plan={"summary": "test", "shots": shots}, video_model_id="video", use_reference_frame=False, **kwargs
    )


def shots(count=6, seconds=2):
    return [
        {
            "start": i * seconds,
            "end": (i + 1) * seconds,
            "observation": "same scene",
            "prompt": f"动作{i}",
            "scene_id": "arena",
        }
        for i in range(count)
    ]


CAPS = {
    "durations": [4, 8, 12],
    "generation_modes": ["full_reference"],
    "reference_limits": {
        "image": {"enabled": True, "max_count": 3},
        "video": {"enabled": True, "max_count": 1},
    },
}


def test_cuts_within_a_passage_are_one_generation_not_six():
    plan = generation_plan(options(shots()), CAPS, [])
    assert len(plan["units"]) == 1
    assert plan["units"][0]["shot_indices"] == [1, 2, 3, 4, 5, 6]
    assert plan["units"][0]["duration"] == 12
    assert plan["units"][0]["use_motion_reference"]


def test_per_shot_identity_binding_and_scene_boundaries():
    refs = [
        {"role": "character", "target": "女主", "purpose": "换脸", "shot_indices": [1, 2]},
        {"role": "scene", "purpose": "森林", "shot_indices": [3, 4]},
    ]
    plan = generation_plan(options(shots(4)), CAPS, refs)
    assert [u["reference_indices"] for u in plan["units"]] == [[0], [1]]
    assert [u["shot_indices"] for u in plan["units"]] == [[1, 2], [3, 4]]
    context = direction_context(plan["units"][1], refs)
    assert context["timeline"][0]["target_start"] == 0
    assert context["timeline"][-1]["target_end"] == 4
    assert "人物身份、脸" not in context["bindings"][0]
    with pytest.raises(ValueError, match="不存在"):
        generation_plan(options(shots(4)), CAPS, [{**refs[0], "shot_indices": [5]}])


def test_continuous_action_carries_tail_only_when_split():
    rows = shots(8)
    rows[6]["boundary"] = "continuous"
    plan = generation_plan(options(rows), CAPS, [])
    assert len(plan["units"]) == 2
    assert plan["units"][1]["continues_previous"]
    assert plan["units"][1]["image_count"] == 1
    caps = {"durations": [4, 8, 12], "generation_modes": ["text_to_video"]}
    with pytest.raises(ValueError, match="连续动作"):
        generation_plan(options(rows), caps, [])


def test_character_photo_is_not_silently_a_first_frame_and_padding_is_visible():
    ref = {"role": "character", "target": "hero", "purpose": "新人物"}
    caps = {"durations": [4, 8], "generation_modes": ["first_frame"]}
    with pytest.raises(ValueError, match="场景参考图模型"):
        generation_plan(options(shots(3, 1)), caps, [ref])
    plan = generation_plan(options(shots(3, 1), image_model_id="image"), caps, [ref])
    assert len(plan["units"]) == 1
    assert plan["units"][0]["prepare_frame"]
    assert plan["output_seconds"] == 4 and plan["source_seconds"] == 3
    assert any("不截掉结尾" in w for w in plan["warnings"])
    with pytest.raises(ValueError, match="填写"):
        generation_plan(options(shots()), CAPS, [{**ref, "target": ""}])


def test_manual_natural_cut_and_role_change_not_lost_to_merging():
    rows = shots(2, 2)
    rows[1]["boundary"] = "cut"
    plan = generation_plan(options(rows), CAPS, [])
    assert len(plan["units"]) == 2
    assert not plan["units"][1]["continues_previous"]
