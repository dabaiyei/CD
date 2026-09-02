from app.services.managed_skills import (
    SYSTEM_PROMPT_CODES,
    default_system_prompt_content,
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
    content = default_system_prompt_content("video-prompt-generation")

    assert "T2VA / I2VA / FL2VA / L2VA" in content
    assert "H3 单次目标时长必须在 4-15 秒内" in content
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
    ref_start = content.index("## H3 全参考模式：Ref2VA")
    ref_contract = content[ref_start : content.index("### 引用定义", ref_start)]
    positions = [ref_contract.index(section) for section in ref_sections]
    assert positions == sorted(positions)
    assert "fully_preserved" in content
    assert "fully_copy" in content
    assert "says in an off-screen voiceover" in content
