from app.services.motion_intent import motion_contract, apply_motion_guidance, NATURAL_MOTION_RULES


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


def test_ordinary_motion_keeps_source_and_does_not_become_combat():
    from app.services.creation_context import ensure_combat_video_prefix
    from app.services.locomotion import apply_locomotion_guidance
    from app.services.speech_pacing import apply_speech_guidance
    original = "固定机位，女孩缓步前行，停下拿起杯子后说你好。"
    result = apply_motion_guidance(original)
    result = apply_locomotion_guidance(result, source=original)
    result = apply_speech_guidance(result, "你好", "语速2级")
    assert result.endswith(original)
    assert "2级慢速" in result and "120—140" in result
    assert ensure_combat_video_prefix(result) == result
    assert result.count(NATURAL_MOTION_RULES) == 1


def test_natural_motion_is_idempotent_and_keeps_act_prefix():
    original = "ACT视角，保留原动作"
    result = apply_motion_guidance(original)
    assert result.startswith("ACT视角，")
    assert apply_motion_guidance(result) == result
    assert result.endswith("保留原动作")


def test_static_silent_clip_does_not_receive_gait_or_speech_blocks():
    from app.services.locomotion import apply_locomotion_guidance, MARKER
    from app.services.speech_pacing import apply_speech_guidance, START
    source = "全画面定格，人物不要行走，固定机位，静音"
    result = apply_locomotion_guidance(apply_motion_guidance(source), source=source)
    result = apply_speech_guidance(result, "你好", audio_enabled=False)
    assert MARKER not in result and START not in result
    assert result.endswith(source)
