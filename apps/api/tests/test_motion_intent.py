from app.services.motion_intent import motion_contract


def test_fixed_camera_does_not_freeze_subject():
    contract = motion_contract("摄影机固定不动；人物向右奔跑")
    assert "摄影机固定不动" in contract
    assert "只对上述指定对象" in contract
    assert "人物向右奔跑" not in contract


def test_static_subject_keeps_timing_and_dialogue_exception():
    contract = motion_contract("前三秒人物静止不动，仅嘴唇说话；后三秒起身")
    assert "前三秒人物静止不动，仅嘴唇说话" in contract
    assert "后三秒起身" not in contract
    assert not motion_contract("人物挥剑，摄影机跟随剑锋")


def test_scene_camera_lock_is_preserved():
    assert "固定机位" in motion_contract("人物说话", "固定机位，教室中景")
