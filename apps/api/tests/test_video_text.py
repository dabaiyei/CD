from app.services.task_worker import enforce_video_audio_policy
from app.services.video_text import ensure_screen_text_locks, video_text_tracks

SOURCE = (
    "【后期文字｜00—03秒】197年初，宛城投降之后。 "
    "【后期文字｜曹操·心中｜03—11秒】这手……这胡子？我穿成曹操了？还是中年版的？"
)


def test_editorial_labels_are_metadata_not_subtitles_or_speech():
    spoken, tracks = video_text_tracks(SOURCE)
    assert spoken == ""
    assert tracks[0] == {"text": "197年初，宛城投降之后。", "timing": "00—03秒", "speaker_context": ""}
    assert tracks[1]["speaker_context"] == "曹操·心中"
    for audio in (True, False):
        prompt = enforce_video_audio_policy(
            SOURCE, SOURCE, audio_enabled=audio, protocol_name="generic", language="zh-CN"
        )
        assert "【后期文字" not in prompt
        assert '"text": "197年初，宛城投降之后。"' in prompt
        assert "只能由对应角色逐句" not in prompt
        assert "不得显示或朗读" in prompt


def test_spoken_dialogue_is_preserved_and_guard_is_idempotent():
    source = SOURCE + "\n曹操：典韦在哪里？"
    spoken, tracks = video_text_tracks(source)
    assert spoken == "曹操：典韦在哪里？"
    assert len(tracks) == 2
    prompt = ensure_screen_text_locks("场景描述", source)
    assert ensure_screen_text_locks(prompt, source) == prompt


def test_silent_h3_keeps_visible_text_contract_in_visual_section():
    prompt = enforce_video_audio_policy(
        "integrated_multimodal_description: scene\noverall_soundscape: quiet\nnon_diegetic_music: none",
        SOURCE,
        audio_enabled=False,
        protocol_name="minimax_h3",
        language="en",
    )
    assert '"text": "197年初，宛城投降之后。"' in prompt
    assert "non_diegetic_music: N/A" in prompt
