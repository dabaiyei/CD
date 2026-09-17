from types import SimpleNamespace

import pytest
from app.db.models import AssetType
from app.services.frame_composition import FrameLayout, build_frame_prompt
from app.services.task_worker import GeneratedStoryboardShotPayload
from app.domain.schemas import StoryboardShotCreate


def layout():
    return dict(camera_position="女子身后偏右，轴线南侧", camera_height="腰部高度，微仰",
        viewing_direction="朝向远处神龙", shot_size="全景", axis_side="对峙轴线南侧",
        subjects=[dict(asset_name="女子", screen_position="左", depth="前景", facing="背对镜头面向右后方神龙",
            gaze_target="神龙头部", pose="左脚前踏，剑尚未抬起", held_items="自身右手握剑")],
        spatial_relations="女子在近处，神龙在十米外，龙头高过城楼", environment_anchors="断柱在画面右侧",
        lighting="左后方暖光", visual_style="项目日式赛璐璐画风")


def assets():
    return [SimpleNamespace(id="hero", name="女子", parent_asset_id=None, asset_type=AssetType.CHARACTER,
        asset_metadata={}, description="正面四视图，已经击中神龙后落地"),
        SimpleNamespace(id="move", name="女子·剑诀", parent_asset_id="hero", asset_type=AssetType.CHARACTER,
        asset_metadata={"combat_technique": {}}, description="连续挥剑，终结后落地")]


def test_structured_layout_overrides_conflicting_free_prompt_and_excludes_asset_actions():
    shot = SimpleNamespace(image_prompt="正面特写，女子击中神龙后落地")
    prompt = build_frame_prompt(shot, assets(), layout())
    assert "女子身后偏右" in prompt and "自身右手握剑" in prompt
    assert "画面左侧/前景" in prompt
    assert "正面特写" not in prompt and "已经击中神龙" not in prompt
    assert "图1：女子" in prompt and "图2：女子·剑诀" in prompt
    assert "同一主体而非另一个人物" in prompt


def test_unknown_layout_asset_is_rejected_before_image_request():
    value = layout()
    value["subjects"][0]["asset_name"] = "陌生人物"
    with pytest.raises(RuntimeError, match="未绑定资产"):
        build_frame_prompt(SimpleNamespace(image_prompt="test"), assets(), value)


def test_spatial_contract_survives_generated_to_create_payload():
    generated = GeneratedStoryboardShotPayload(title="对峙", image_prompt="女子身后观察神龙",
        asset_names=["女子"], frame_layout=layout())
    create = StoryboardShotCreate(**generated.model_dump(exclude={"asset_names"}), asset_ids=["hero"])
    stored = create.model_dump(mode="json")
    assert FrameLayout.model_validate(stored["frame_layout"]).subjects[0].screen_position == "左"


def test_legacy_prompt_keeps_user_content_but_not_motion_from_asset_descriptions():
    prompt = build_frame_prompt(SimpleNamespace(image_prompt="女子背对镜头立于前景"), assets())
    assert "女子背对镜头立于前景" in prompt
    assert "已经击中神龙" not in prompt
    assert "不能都朝镜头摆拍" in prompt
