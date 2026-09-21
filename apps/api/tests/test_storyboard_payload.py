"""The storyboard board must not ship prompt bodies the user never opened."""
from app.domain.schemas import StoryboardShotPrompts, StoryboardShotPublic, StoryboardShotSummary


def fake_shot(**overrides):
    from datetime import UTC, datetime
    from decimal import Decimal

    values = dict(
        id="shot-1", storyboard_version_id="board-1", order_index=1, title="雨夜推门",
        shot_type="中景", duration_seconds=Decimal("5"), scene_description="修复室",
        action_description="她推门进入", dialogue="", image_prompt="首帧提示词" * 200,
        video_prompt="视频提示词" * 800, asset_ids=["a1"], reference_image_url=None,
        version=3, created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    values.update(overrides)
    from types import SimpleNamespace
    return SimpleNamespace(**values)


def test_the_summary_omits_the_prompt_bodies():
    summary = StoryboardShotSummary.from_shot(fake_shot())
    dumped = summary.model_dump()
    assert "video_prompt" not in dumped
    assert "image_prompt" not in dumped
    # Everything the board renders is still there.
    assert dumped["title"] == "雨夜推门"
    assert dumped["action_description"] == "她推门进入"
    assert dumped["asset_ids"] == ["a1"]


def test_the_summary_reports_whether_each_prompt_exists():
    assert StoryboardShotSummary.from_shot(fake_shot()).has_video_prompt is True
    assert StoryboardShotSummary.from_shot(fake_shot()).has_image_prompt is True
    blank = StoryboardShotSummary.from_shot(fake_shot(video_prompt="   ", image_prompt=""))
    assert blank.has_video_prompt is False
    assert blank.has_image_prompt is False


def test_the_summary_is_much_smaller_than_the_full_shot():
    shot = fake_shot()
    summary_bytes = len(StoryboardShotSummary.from_shot(shot).model_dump_json())
    full_bytes = len(StoryboardShotPublic.model_validate(shot).model_dump_json())
    # The prompt bodies dominate the payload, so the board shrinks by most of it.
    assert summary_bytes < full_bytes * 0.2


def test_the_prompts_payload_carries_only_that_shot():
    prompt = StoryboardShotPrompts(id="shot-1", version=3, image_prompt="首帧", video_prompt="视频")
    assert set(prompt.model_dump()) == {"id", "version", "image_prompt", "video_prompt"}
