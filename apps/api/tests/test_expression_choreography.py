import pytest

from app.services import expression_skill_retrieval as retrieval
from app.services.expression_choreography import (
    EmotionBeat,
    EmotionPlan,
    audit,
    beats,
    expression_contract,
    inject_contract,
)


def shot(**overrides):
    row = {
        "shot_id": "shot-1",
        "duration_seconds": 8,
        "title": "雨夜对峙",
        "shot_type": "中近景",
        "scene_description": "雨夜院门前，冷蓝光",
        "action_description": "他慢慢抬起头",
        "dialogue": "",
        "assets": [{"name": "男主", "asset_type": "character", "description": "青衫少年"}],
        "reference_map": [],
    }
    row.update(overrides)
    return row


def test_library_parses_all_sixty_entries_into_six_categories():
    entries = retrieval.library()
    assert len(entries) == 60
    assert len(set(retrieval.EMOTION_FILES.values())) == 6
    assert all(name in entries for name in retrieval.EMOTION_FILES)
    assert retrieval.performance("隐忍难过").startswith("眼眶微红")


def test_retrieval_opens_only_the_categories_this_shot_performs():
    context, manifest = retrieval.retrieve(shot(emotion_plan={"beats": [
        {"start": 0, "end": 3, "character": "男主", "emotion": "强装镇定"},
        {"start": 3, "end": 8, "character": "男主", "emotion": "含泪愤怒"},
    ]}))
    assert manifest["selected"] == ["core.md", "emotion-basic.md", "emotion-anger.md"]
    assert manifest["skipped"] == []
    assert "平静克制" in context
    # Categories this shot does not perform must not be opened at all: a grief
    # entry would only crowd the request.
    for filename in ("emotion-grief.md", "emotion-joy.md", "emotion-fear.md", "emotion-complex.md"):
        assert f'name="{filename}"' not in context
    assert "悔恨自责" not in context
    assert "浅浅开心" not in context
    assert manifest["origin"] == "declared"
    assert manifest["characters"] <= retrieval.MAX_SKILL_CHARS


def test_inference_maps_free_text_to_a_library_emotion():
    assert retrieval.infer_emotion("他听到真相后愣在原地") == "发现真相"
    # A literal library name wins over the broader inference pattern, so the
    # more precise entry is used instead of being folded into its neighbour.
    assert retrieval.infer_emotion("她终于松了一口气") == "松了一口气"
    assert retrieval.infer_emotion("他终于释然了") == "释然一笑"
    # No emotional cue still yields a performable baseline rather than nothing.
    assert retrieval.infer_emotion("画面只有一张空桌") == "平静克制"


def test_every_library_name_routes_to_itself():
    """Ten distinct complex emotions used to collapse into 下定决心, which
    silently replaced the performance the caller named."""
    wrong = {name: retrieval.infer_emotion(name)
             for name in retrieval.EMOTION_FILES if retrieval.infer_emotion(name) != name}
    assert wrong == {}


def test_declared_timeline_maps_unknown_names_and_validates_duration():
    plan = EmotionPlan.model_validate({"beats": [
        {"start": 0, "end": 2, "character": "男主", "emotion": "强装镇定"},
        {"start": 2, "end": 8, "character": "男主", "emotion": "说不出名字的复杂心绪",
         "intensity": "extreme", "trigger": "看见玉佩"},
    ]})
    # One invented word must not fail the board, and it must not be silently
    # rewritten either: the source skill's answer for an emotion outside the
    # sixty is the universal formula, not a nearest-entry substitution.
    assert plan.beats[1].emotion == "说不出名字的复杂心绪"
    # Retrieval still routes it to a real entry so the reference block and the
    # English lock have something valid to open.
    routed = retrieval.canonical_emotion(plan.beats[1].emotion, plan.beats[1].trigger)
    assert routed in retrieval.EMOTION_FILES
    assert routed != plan.beats[1].emotion
    plan.validate_duration(8)
    with pytest.raises(ValueError):
        plan.validate_duration(5)


def test_plan_rejects_overlapping_beats_and_bad_intensity():
    with pytest.raises(ValueError):
        EmotionPlan.model_validate({"beats": [
            {"start": 0, "end": 4, "character": "男主", "emotion": "平静克制"},
            {"start": 3, "end": 6, "character": "男主", "emotion": "浅浅开心"},
        ]})
    with pytest.raises(ValueError):
        EmotionBeat.model_validate({"start": 0, "end": 2, "character": "男主",
                                    "emotion": "平静克制", "intensity": "huge"})


def test_shot_without_a_plan_infers_a_whole_shot_performance():
    found, origin = beats(shot(action_description="他听到真相，眼睛慢慢睁大"))
    assert origin == "inferred"
    assert len(found) == 1
    assert found[0]["character"] == "男主"
    assert found[0]["start"] == 0 and found[0]["end"] == 8


def test_no_character_shot_never_gets_a_face():
    assert beats(shot(assets=[], reference_map=[], dialogue="", title="空镜")) == ([], "none")
    assert expression_contract(shot(assets=[], reference_map=[], title="空镜")) == ""


def test_contract_compiles_the_library_sentence_and_keeps_the_trigger():
    row = shot(emotion_plan={"beats": [
        {"start": 3, "end": 8, "character": "男主", "emotion": "发现真相",
         "intensity": "extreme", "trigger": "看见女主手里的玉佩"},
    ]})
    text = expression_contract(row)
    assert "男主在3–8s" in text
    assert "发现真相" in text
    assert "眼睛慢慢睁大" in text
    assert "看见女主手里的玉佩" in text
    assert "唯一允许放开" in text


def test_contract_is_skipped_when_the_reply_already_performed_the_beat():
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "隐忍难过"},
    ]})
    performed = "男主眼眶微红，嘴角压住，眼神向下，像在努力不让眼泪掉下来。"
    assert expression_contract(row, existing=performed) == ""
    # A bare label is not a performance, so the contract still fills one in.
    assert expression_contract(row, existing="男主隐忍难过。") != ""
    assert audit(performed, row) == []
    assert audit("", row) == ["隐忍难过"]


def test_contract_follows_the_model_language():
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 4, "character": "男主", "emotion": "忍着怒气"},
    ]})
    assert "本镜人物表演要求" in expression_contract(row, language="zh-CN")
    english = expression_contract(row, language="en-US")
    assert "Performance locks for this shot" in english
    assert "medium" in english
    # H3 and other English protocols must not receive Chinese library prose in
    # the final prompt, so the lock is translated rather than pasted through.
    assert "suppressed anger" in english
    assert "忍着怒气" not in english
    assert "下颌收紧" not in english


def test_chinese_contract_separates_the_trigger_from_the_strength_note():
    """Bare concatenation glued "触发：…" onto the strength sentence, turning
    two requirements into one run-on clause the video model misreads."""
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 3, "character": "男主", "emotion": "强装镇定",
         "intensity": "slight", "trigger": "听到对方威胁"},
    ]})
    text = expression_contract(row)
    assert "触发：听到对方威胁；只给细微变化" in text
    assert "听到对方威胁只给细微变化" not in text


def test_english_contract_never_leaks_the_chinese_trigger_into_the_body():
    """The trigger is Chinese story context. Carrying it into an English
    protocol body is the mixed-language prose that makes those models drift
    into invented languages and stray audible speech."""
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 3, "character": "男主", "emotion": "含泪愤怒",
         "intensity": "extreme", "trigger": "想起三年前的灭门旧事"},
    ]})
    english = expression_contract(row, language="en-US")
    assert "想起三年前的灭门旧事" not in english
    body = "\n".join(line for line in english.splitlines() if line.startswith("男主"))
    assert "tearful anger" in body
    assert "Trigger:" in body


def test_english_reply_that_already_performed_the_beat_is_not_repeated():
    """H3 writes the performance in English, so a Chinese keyword match would
    always miss and append a duplicate lock on top of a correct performance."""
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "忍着怒气"},
    ]})
    performed = ("At 0.00 seconds the man holds suppressed anger: his gaze locks forward, "
                 "his eyebrows press down and his lips tighten while his shoulders stiffen.")
    assert expression_contract(row, language="en-US", existing=performed) == ""
    assert audit(performed, row, language="en-US") == []
    assert expression_contract(row, language="en-US", existing="He is angry.") != ""




def test_version_marker_tracks_the_retrieved_reference_hashes():
    _, first = retrieval.retrieve(shot(emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "忍着怒气"}]}))
    _, second = retrieval.retrieve(shot(emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "浅浅开心"}]}))
    assert first["hashes"] != second["hashes"]
    assert retrieval.version_marker(first) != retrieval.version_marker(second)
    assert retrieval.version_marker(first) == retrieval.version_marker(dict(first))


def test_guidance_carries_the_five_element_formula():
    text = retrieval.guidance()
    for word in ("眼神", "眉毛", "嘴角", "身体反应", "剧情状态"):
        assert word in text
    assert "嘴唇保持完全闭合" in text
    assert "库中没有的情绪用万能表情公式" in text


def test_core_keeps_the_full_universal_formula_and_its_workflow():
    """The source skill's universal formula is not just the five elements; it
    also carries the creation workflow, the template and a worked example. A
    trimmed core would leave the model without the steps that make an invented
    emotion performable."""
    core = retrieval._read("core.md")
    for word in ("万能表情公式", "眼神", "眉毛", "嘴角", "身体反应", "剧情状态",
                 "像", "强度", "角色设定", "[情绪名]", "自查", "职业性冷静"):
        assert word in core, word


def test_retrieval_degrades_instead_of_failing_when_the_budget_is_tight(monkeypatch):
    """A single shot must not fail because two categories together overflow.

    The compiled lock only needs the entry it names, so dropping the later
    category still ships a valid performance; losing everything still raises.
    """
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 3, "character": "男主", "emotion": "强装镇定"},
        {"start": 3, "end": 8, "character": "男主", "emotion": "含泪愤怒"},
    ]})
    def block(filename):
        content = retrieval._read(filename)
        return f'<expression-reference name="{filename}">\n{content}\n</expression-reference>'

    budget = len(block("core.md")) + 2 + len(block("emotion-basic.md")) + 1
    monkeypatch.setattr(retrieval, "MAX_SKILL_CHARS", budget)
    context, manifest = retrieval.retrieve(row)
    assert manifest["selected"] == ["core.md", "emotion-basic.md"]
    assert manifest["skipped"] == ["emotion-anger.md"]
    assert "强装镇定" in context
    assert "含泪愤怒" not in context

    monkeypatch.setattr(retrieval, "MAX_SKILL_CHARS", 10)
    with pytest.raises(ValueError):
        retrieval.retrieve(row)


def test_distant_framing_moves_the_performance_into_the_body():
    """A wide shot cannot read a face. Pushing eyebrows and lips there would
    make the video model cut in for a close-up the storyboard never asked for."""
    row = shot(shot_type="大全景", emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "隐忍难过"},
    ]})
    text = expression_contract(row)
    assert "景别较远" in text
    assert "不要为看清表情而改变已确定的景别或机位" in text

    english = expression_contract(row, language="en-US")
    assert "framed at a distance" in english
    assert "changing the established framing" in english

    assert "景别较远" not in expression_contract(shot(shot_type="面部特写", emotion_plan={
        "beats": [{"start": 0, "end": 8, "character": "男主", "emotion": "隐忍难过"}]}))


def test_inferred_performance_on_a_wide_shot_stays_light():
    found, origin = beats(shot(shot_type="远景", action_description="他站在城墙上"))
    assert origin == "inferred"
    assert found[0]["intensity"] == "slight"
    # A close shot without a plan still gets the normal middle intensity.
    close, _ = beats(shot(shot_type="中近景", action_description="他站在城墙上"))
    assert close[0]["intensity"] == "medium"


def test_lock_is_injected_inside_the_described_body():
    """Structured protocols end on the audio fields; appending after them would
    leave stray prose inside `non_diegetic_music`."""
    h3 = ("integrated_multimodal_description: [Shot 1] The room is quiet.\n"
          "overall_soundscape: N/A\nnon_diegetic_music: N/A")
    result = inject_contract(h3, "LOCK")
    assert result.count("LOCK") == 1
    assert result.index("LOCK") < result.index("overall_soundscape:")
    assert result.endswith("non_diegetic_music: N/A")

    ref2va = "subject_definitions:\nA\n\ndetailed_description:\nHe walks.\n\noverall_soundscape: N/A"
    placed = inject_contract(ref2va, "LOCK")
    assert placed.index("LOCK") < placed.index("overall_soundscape:")
    # A plain prompt with no protocol field still gets the lock.
    assert inject_contract("plain", "LOCK").endswith("LOCK")
    assert inject_contract("plain", "") == "plain"


def test_silent_task_seals_the_lips_in_both_languages():
    """The lock names mouth and lips, so an audio-disabled task must state that
    they stay sealed. Without this the model can read the lock as licence to
    speak, which is the stray-audio failure the project already guards against."""
    row = shot(audio_enabled=False, emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "忍着怒气"},
    ]})
    zh = expression_contract(row)
    assert "嘴唇必须完全闭合" in zh
    assert "关闭音频" in zh

    en = expression_contract(row, language="en-US")
    assert "lips stay completely closed" in en
    assert "no speech" in en

    # An audio-enabled shot keeps the performance without the silent note.
    loud = shot(audio_enabled=True, emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "忍着怒气"},
    ]})
    assert "嘴唇必须完全闭合" not in expression_contract(loud)
    # A row that never states the switch keeps the old, non-silent behaviour.
    assert "嘴唇必须完全闭合" not in expression_contract(shot(
        emotion_plan={"beats": [{"start": 0, "end": 8, "character": "男主", "emotion": "忍着怒气"}]}))


def test_emotion_outside_the_library_uses_the_universal_formula():
    """The source skill is library-first: use one of the sixty when it fits, and
    only then invent an emotion outside them and perform it with the universal
    formula. Replacing the invented name with the nearest library entry would
    silently change the performance the storyboard asked for."""
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 6, "character": "男主", "emotion": "狂喜",
         "intensity": "extreme", "trigger": "苦等十年的仇人终于伏法"},
    ]})
    text = expression_contract(row)
    assert "狂喜" in text
    assert "万能表情公式" in text
    assert "最接近的库内参照" in text
    # The plain wrong answer: silently reporting a different emotion.
    assert "情绪是“平静克制”" not in text

    # A five-element performance of the invented emotion counts as performed, so
    # no duplicate lock is appended on top of a correct reply.
    reply = ("男主眼神瞬间发亮，瞳孔放大，眉毛高高扬起，嘴唇张开又合上，"
             "肩膀因为激动而抬起，胸口剧烈起伏，像卸下了压了十年的石头。")
    assert expression_contract(row, existing=reply) == ""
    # A bare label is still not a performance.
    assert expression_contract(row, existing="男主狂喜。") != ""


def test_english_lock_for_an_invented_emotion_names_no_wrong_emotion():
    """The invented name is Chinese and has no English equivalent, so the English
    lock must point at the shot data rather than substitute a library emotion."""
    row = shot(emotion_plan={"beats": [
        {"start": 0, "end": 6, "character": "男主", "emotion": "狂喜",
         "intensity": "extreme", "trigger": "大仇得报"},
    ]})
    text = expression_contract(row, language="en-US")
    assert "declared for this beat in the shot data" in text
    assert "universal" in text.lower()
    # The nearest entry may be referenced as styling, but never as a replacement.
    assert "not a replacement" in text
    # No Chinese prose leaks into the English lock body.
    body = "\n".join(line for line in text.splitlines() if line.startswith("男主"))
    assert "狂喜" not in body


def test_per_shot_request_carries_the_performance_reference():
    """The wiring, not just the module: one shot's request must include the
    categories it performs plus the hard rules, so the video model actually
    receives the library instead of only the system prompt."""
    import asyncio

    from app.services.task_worker import TaskRuntimeContext, runtime_request_from_context

    row = shot(shot_id="shot-9", order_index=1, emotion_plan={"beats": [
        {"start": 0, "end": 8, "character": "男主", "emotion": "强装镇定"}]})
    context = TaskRuntimeContext(
        tenant_id="t", project_id="p", task_id="task-1",
        system_prompt_head="HEAD", system_prompt_tail="TAIL", skill_context="",
        model_binding={"model": "m"}, prompt_versions={}, skill_versions={}, skills=[],
        template_content="TEMPLATE", memory_enabled=False, memory_user_id="u",
    )
    request = asyncio.run(
        runtime_request_from_context(context, "BASE\n", row, protocol_appendix="")
    )
    assert "本镜按需表演参考：" in request.prompt
    assert 'name="core.md"' in request.prompt
    assert 'name="emotion-basic.md"' in request.prompt
    # The library entry for the declared emotion travels with the shot.
    assert "脸上保持平静，眼底却有细微紧张" in request.prompt
    # And an unrelated category is not pulled in.
    assert 'name="emotion-grief.md"' not in request.prompt
