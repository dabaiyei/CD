from app.db.models import HandbookType
from app.services.managed_skills import (
    HANDBOOK_TASK_FILES,
    SYSTEM_PROMPT_CODES,
    default_system_prompt_content,
    internal_system_prompt_content,
)


def test_system_prompt_templates_are_complete_and_runtime_compatible() -> None:
    forbidden_legacy_calls = {
        "get_planData",
        "get_novel_events",
        "get_novel_text",
        "get_script_content",
        "get_flowData",
        "resultTool",
        "run_sub_agent_",
        "run_supervision_agent",
        "deepRetrieve",
        "insert_script_to_sqlite",
    }

    for code in SYSTEM_PROMPT_CODES:
        content = default_system_prompt_content(code)
        assert len(content) >= 500, code
        assert "JSON" in content, code
        assert not any(name in content for name in forbidden_legacy_calls), code


def test_video_prompt_template_preserves_h3_official_mode_contracts() -> None:
    generic = default_system_prompt_content("video-prompt-generation")
    content = internal_system_prompt_content("video-prompt-generation-h3.md")

    assert "name` 为 `generic`" in generic
    assert "不得使用 MiniMax H3" in generic
    assert "integrated_multimodal_description:" not in generic
    assert "T2VA / I2VA / FL2VA / L2VA" in content
    assert "必须服从平台本次提供的完整执行契约" in content
    assert (
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced."
    ) in content
    assert "integrated_multimodal_description:" in content
    assert "overall_soundscape:" in content
    assert "non_diegetic_music:" in content

    ref_sections = [
        "subject_definitions:",
        "summary:",
        "retention_analysis:",
        "detailed_description:",
        "overall_soundscape:",
        "non_diegetic_music:",
    ]
    ref_start = content.index("## Ref2VA 正文")
    ref_contract = content[ref_start : content.index("## 台词与连续性", ref_start)]
    positions = [ref_contract.index(section) for section in ref_sections]
    assert positions == sorted(positions)
    assert "fully_preserved" in content
    assert "fully_copy" in content
    assert "says in an off-screen voiceover" in content


def test_storyboard_prompts_define_duration_decisions_and_required_handbooks() -> None:
    generation = default_system_prompt_content("storyboard-generation")
    review = default_system_prompt_content("storyboard-review")
    repair = default_system_prompt_content("storyboard-repair")

    assert "不把最短时长当默认值" in generation
    assert "短档用于" in generation
    assert "大多数镜头" in review
    assert "平台模型时长约束优先" in repair
    assert '"video_prompt":""' in generation
    assert '"video_prompt":""' in repair
    assert HANDBOOK_TASK_FILES["storyboard-generation"][HandbookType.VISUAL] == (
        "README.md",
        "prefix.md",
        "storyboard.md",
        "technique-director-rules.md",
        "technique-storyboard-table-design.md",
    )
    assert HANDBOOK_TASK_FILES["storyboard-generation"][HandbookType.DIRECTOR] == (
        "README.md",
        "director-planning.md",
        "storyboard-table.md",
    )
