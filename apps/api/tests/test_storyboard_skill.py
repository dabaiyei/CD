from decimal import Decimal
from pathlib import Path

from app.services.storyboard_quality import (
    CHARS_PER_SECOND,
    _dialogue_seconds,
    evaluate,
    merge,
)
from app.services.storyboard_skill_retrieval import (
    MAX_SKILL_CHARS,
    ROOT,
    SOURCE,
    SOURCE_REVISION,
    guidance,
    retrieve,
    select_files,
)

STAGES = (
    "storyboard-generation",
    "storyboard-review",
    "storyboard-repair",
    "video-prompt-generation",
)


def test_skill_pack_is_attributed_and_licensed():
    # Apache-2.0 requires the licence and notice to travel with derived content.
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "Apache License" in licence
    assert "Version 2.0" in licence
    assert SOURCE in skill
    assert SOURCE_REVISION in skill
    assert "Apache-2.0" in skill
    # The pack must state what was deliberately not absorbed, so a later reader
    # does not assume the upstream schema and gate files were merged.
    assert "没有吸收" in skill


def test_skill_pack_is_only_loaded_by_storyboard_stages():
    for stage in STAGES:
        assert select_files(stage, "资产 参考图 台词 运镜"), stage
    for stage in ("script-generation", "asset-prompt-generation", "chapter-analysis", "dialogue-extraction"):
        assert select_files(stage, "资产 参考图 台词 运镜") == [], stage


def test_stage_routing_never_loads_the_whole_pack():
    pack = {path.name for path in ROOT.glob("*.md")}
    for stage in STAGES:
        selected = set(select_files(stage, "资产 参考图 台词 运镜 h3 声景 分镜"))
        assert selected <= pack, stage
        assert selected != pack, stage


def test_retrieval_is_bounded_and_reports_hashes():
    # Every stage must stay inside the budget for every routing outcome, because
    # retrieve() raises on overflow and that would fail a live task.
    queries = ("", "资产 参考图 台词 运镜 h3 声景 分镜 首帧 尾帧 人物 场景 道具")
    for stage in STAGES:
        for query in queries:
            context, manifest = retrieve(stage, query)
            assert context, (stage, query)
            assert len(context) <= MAX_SKILL_CHARS, (stage, query)
            assert manifest["source"] == SOURCE
            assert manifest["revision"] == SOURCE_REVISION
            assert manifest["characters"] == len(context)
            assert set(manifest["hashes"]) == set(manifest["selected"])
            for digest in manifest["hashes"].values():
                assert len(digest) == 64


def test_unsupported_stage_returns_nothing():
    assert retrieve("script-generation", "任何内容") == ("", {})
    assert guidance("script-generation") == ""


def test_guidance_subordinates_the_method_to_platform_contracts():
    text = guidance("storyboard-generation")
    assert SOURCE in text
    assert "最高优先级硬约束" in text
    assert "静止" in text
    assert "战斗专项规则" in text
    # The adapted method must not claim authority over story facts.
    assert "不改剧本事实" in text


def test_h3_supplement_does_not_redeclare_the_protocol():
    """Overlapping the existing H3 contract is the main conflict risk."""
    supplement = (ROOT / "h3-prompt.md").read_text(encoding="utf-8")
    # No second field structure and no second alignment sentence.
    for section in ("subject_definitions:", "retention_analysis:", "detailed_description:"):
        assert section not in supplement, section
    assert "How the reference pictures align with the target video —" not in supplement
    # It must defer to the authoritative protocol.
    assert "video-prompt-generation-h3.md" in supplement
    assert "以那份协议为准" in supplement
    assert "audio_enabled_for_this_task" in supplement


def test_cutting_reference_does_not_hardcode_model_limits():
    cutting = (ROOT / "cutting.md").read_text(encoding="utf-8")
    # Segment/beat schema and prompt-language switches were deliberately not
    # absorbed, and fixed seconds must never read as a model contract.
    for forbidden in ("maxSegmentSeconds", "promptLang", "beats:[起,止]"):
        assert forbidden not in cutting, forbidden
    assert "合法档位" in cutting


def test_dialogue_counting_matches_the_duration_model():
    assert _dialogue_seconds("") == 0
    # The speaker label is not spoken; the words and their pauses are.
    assert _dialogue_seconds("阿贝尔：魔法阵。") == Decimal(4) / CHARS_PER_SECOND
    # Punctuation is pause time and therefore counts.
    assert _dialogue_seconds("你好，世界。") == Decimal(6) / CHARS_PER_SECOND
    # Bracketed stage directions are not spoken.
    assert _dialogue_seconds("甲：你好（转身）世界。") == Decimal(5) / CHARS_PER_SECOND
    # Numerals are read out, so every digit is time.
    assert _dialogue_seconds("第12章") == Decimal(4) / CHARS_PER_SECOND


def test_dialogue_counting_ignores_on_screen_text():
    """Subtitles are displayed, not spoken, and must never inflate the estimate."""
    assert _dialogue_seconds("字幕：服务器瘫痪。") == 0
    assert _dialogue_seconds("【后期文字｜00—03秒】197年初，宛城投降之后。") == 0
    # Spoken dialogue on a later line still counts.
    counted = _dialogue_seconds("字幕：服务器瘫痪。\n苏郁：我知道。")
    assert counted == Decimal(4) / CHARS_PER_SECOND


def test_dialogue_gate_reports_only_clear_overruns():
    crowded = [{"order_index": 5, "duration_seconds": 6, "dialogue": "阿贝尔：魔法阵，要不要约会？可以去图书馆、图书馆……或者图书馆。阿贝尔（旁白）：啊，这里就是我的座位。无处可逃。"}]
    findings = evaluate(crowded)
    assert len(findings) == 1
    assert findings[0]["severity"] == "minor"
    assert "镜头 5" in findings[0]["location"]

    # A line that comfortably fits is silent.
    assert evaluate([{"order_index": 1, "duration_seconds": 6, "dialogue": "阿贝尔：好。"}]) == []


def test_dialogue_gate_tolerates_narration_density():
    """The project's own boards sit at a ratio near 1.4; that must not be flagged."""
    shots = [
        {"order_index": 1, "duration_seconds": 6, "dialogue": "阿贝尔（旁白）：大家好，我是一位见习魔法使，也是一位不受欢迎的阴沉边缘男子，十六岁。"},
        {"order_index": 2, "duration_seconds": 6, "dialogue": "阿贝尔：魔法阵，要不要约会？可以去图书馆、图书馆……或者图书馆。"},
    ]
    assert evaluate(shots) == []


def test_uniform_pacing_gate_needs_a_real_pattern():
    uniform = [{"order_index": i, "duration_seconds": 5, "dialogue": ""} for i in range(1, 9)]
    findings = evaluate(uniform)
    assert any("均匀病" in item["issue"] for item in findings)
    varied = [{"order_index": i, "duration_seconds": 4 + (i % 5), "dialogue": ""} for i in range(1, 9)]
    assert evaluate(varied) == []
    # A short board has no rhythm to judge.
    assert evaluate(uniform[:4]) == []


def test_merge_keeps_the_model_verdict_and_findings():
    review = {
        "approved": False,
        "summary": "模型结论",
        "findings": [{"severity": "blocking", "location": "镜头 1", "issue": "模型的阻断项", "suggestion": "修"}],
    }
    shots = [{"order_index": i, "duration_seconds": 5, "dialogue": ""} for i in range(1, 9)]
    merged = merge(review, shots)
    assert merged["approved"] is False
    assert merged["summary"] == "模型结论"
    assert any(item["issue"] == "模型的阻断项" for item in merged["findings"])
    assert any("均匀病" in item["issue"] for item in merged["findings"])


def test_merge_is_advisory_and_deduplicates():
    """The gates surface evidence; they must not overturn an approval."""
    # Ratio is about 1.7x: a clear overrun that is still only advisory.
    shots = [{"order_index": 1, "duration_seconds": 5, "dialogue": "甲：这句话按常规语速在五秒内肯定念不完，必须延长时长或者拆到相邻的镜头里去处理才行。"}]
    base = evaluate(shots)
    assert base
    merged = merge({"approved": True, "summary": "通过", "findings": []}, shots)
    assert merged["approved"] is True
    # Running twice must not duplicate the same finding.
    again = merge(merged, shots)
    assert len(again["findings"]) == len(merged["findings"])


def test_every_reference_file_is_loadable_utf8():
    for path in sorted(ROOT.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert text.strip(), path.name
        assert "\ufffd" not in text, path.name


def test_gates_never_fail_a_review_on_malformed_shot_data():
    """The review path must survive junk instead of erroring the whole task."""
    junk = [
        {"order_index": 1, "duration_seconds": "not-a-number", "dialogue": "甲：你好。"},
        {"order_index": 2, "duration_seconds": None, "dialogue": "甲：你好。"},
        {"order_index": 3, "dialogue": "甲：你好。"},
        {"order_index": 4, "duration_seconds": 5, "dialogue": None},
        {},
    ]
    assert evaluate(junk) == []
    merged = merge({"approved": True, "summary": "通过", "findings": []}, junk)
    assert merged["approved"] is True
    assert merged["findings"] == []
