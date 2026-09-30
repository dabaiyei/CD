import asyncio
import json

from app.services.cinematography import (
    CORE_RULES,
    FRAMING,
    IMAGING_RULES,
    PLANNING_RULES,
    REVIEW_RULES,
    VIDEO_RULES,
    VISUAL_RULES,
    scene_guidance,
    stage_guidance,
)


def test_camera_rules_are_stage_scoped_and_bounded():
    assert stage_guidance("storyboard-generation") == PLANNING_RULES
    assert stage_guidance("video-prompt-generation") == VIDEO_RULES
    assert stage_guidance("storyboard-review") == REVIEW_RULES
    assert "镜头语言" not in stage_guidance("script-generation")
    assert stage_guidance("asset-prompt-generation") == ""
    assert len(VIDEO_RULES) < 2500
    assert len(PLANNING_RULES) < 2600
    assert len(VISUAL_RULES) < 650
    assert len(REVIEW_RULES) < 400
    assert "不是Tilt" in CORE_RULES and "俯仰不是升降" in CORE_RULES
    assert "景别与运镜独立" in FRAMING
    assert "取景边界为准" in FRAMING


def test_reference_budget_and_scene_evidence_excludes_unrelated_asset_prompts():
    source = {
        "action_description": "两个人交谈，主角拿起纸条查看线索",
        "assets": [{"generation_prompt": "鬼怪追杀，恐怖打斗"}],
    }
    rules = scene_guidance(source)
    assert "对话：" in rules and "物件/线索：" in rules
    assert "悬疑恐怖：" not in rules and "动作/追逐：" not in rules
    all_scenes = scene_guidance("对话恐怖打斗落泪巨物道具日常", limit=99)
    assert len(all_scenes) < 450
    assert "情感：" not in all_scenes
    assert scene_guidance("抽象色彩变化") == ""


def test_battle_camera_details_survive_prompt_compilation():
    from app.services.combat_choreography import CombatDesign, compile_prompt

    camera = "全景完整头脚，腰高侧跟双方同速向右；接触时保持轴线南侧，结束停在石柱旁"
    design = CombatDesign.model_validate(
        {
            "plan": {
                "windows": [
                    {
                        "start": 0,
                        "end": 4,
                        "balance": "balanced",
                        "participants": ["甲", "乙"],
                        "objective": "挡住突刺",
                        "outcome": "双方分开",
                    }
                ]
            },
            "setting": "宫殿",
            "identity_lock": "沿用参考衣着",
            "beats": [
                {
                    "start": 0,
                    "end": 4,
                    "phase": "exchange",
                    "actions": ["甲突刺，乙格挡后侧撤"],
                    "camera": camera,
                    "lighting": "主光保持左后侧，柔光高反差，暖灯冷天光；接触火花不遮脸",
                    "continuity": "乙右脚落地，重心向右",
                    "impact": "剑锋反弹",
                    "expression": "目光锁定对手",
                }
            ],
            "end_state": "两人分开站在石柱同侧",
        }
    )
    for name in ("generic", "minimax_h3"):
        prompt = compile_prompt(design, {"name": name, "preferred_prompt_language": "zh-CN"})
        assert camera in prompt and "0–4s" in prompt
        assert "主光保持左后侧，柔光高反差，暖灯冷天光；接触火花不遮脸" in prompt
        assert "两人分开站在石柱同侧" in prompt


def test_home_unrelated_chat_does_not_load_camera_reference():
    from app.services.personal_creation_guidance import personal_creation_guidance

    assert personal_creation_guidance("今天星期几") == ""
    assert personal_creation_guidance("生成一张白底人物设定图", mode="image") == ""
    rules = personal_creation_guidance("写固定机位的悬疑视频提示词：两个人对话，人物不动")
    assert "【镜头语言】" in rules
    assert "固定机位" in rules and "人物静止不等于相机静止" in rules
    assert VISUAL_RULES in rules and IMAGING_RULES in rules
    assert "不每句台词机械切一次" in rules


def test_noncombat_video_request_keeps_camera_rules_and_original_shot():
    from app.services.task_worker import TaskRuntimeContext, runtime_request_from_context

    context = TaskRuntimeContext(
        tenant_id="t",
        project_id="p",
        task_id="task",
        system_prompt_head=VIDEO_RULES,
        system_prompt_tail="",
        skill_context="",
        model_binding={"model": "fake"},
        prompt_versions={},
        skill_versions={},
        skills=[],
        template_content="",
        memory_enabled=False,
        memory_user_id="u",
    )
    row = {
        "shot_id": "s",
        "order_index": 1,
        "action_description": "静谧空镜：摄影机固定，树叶随风摆动",
        "dialogue": "",
        "jev_modules": {"combat": "none", "emotion": "no", "locomotion": "no", "speech": "no"},
    }
    request = asyncio.run(
        runtime_request_from_context(context, base_prompt="镜头数据：", row=row, protocol_appendix="保持4秒")
    )
    assert request.system_prompt.count("【镜头语言】") == 1
    assert "日常/观察：" in request.system_prompt and "动作/追逐：" not in request.system_prompt
    assert json.loads(request.prompt.split("镜头数据：", 1)[1]) == [row]
    rules = next(file.content for file in request.project_files if file.id == "retrieval-task-rules")
    assert "保持4秒" in rules and VIDEO_RULES in rules
    assert VISUAL_RULES in rules and IMAGING_RULES in rules
    # Lighting/color/imaging are instructions, not new mandatory output/API fields.
    assert set(json.loads(request.prompt.split("镜头数据：", 1)[1])[0]) == set(row)
