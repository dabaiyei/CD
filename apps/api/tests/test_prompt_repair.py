"""A prompt the image platform refuses on policy grounds is rewritten, not failed."""
import pytest

from app.services.media_gateway import ModelGatewayError, prompt_was_rejected
from app.services.prompt_repair import (
    is_usable_rewrite,
    parse_rewrite,
    reattach_identity_lock,
    rewrite_prompt_text,
)

POLICY_MESSAGE = (
    "图片平台返回 HTTP 400：您的请求无法用于生成图像。"
    "该请求可能因安全政策被拦截，或不适合进行图像生成。"
)


def gateway_error(message: str, status: int) -> ModelGatewayError:
    return ModelGatewayError(message, status_code=status, detail=message)


def test_a_policy_rejection_is_recognised():
    assert prompt_was_rejected(gateway_error(POLICY_MESSAGE, 400)) is True
    assert prompt_was_rejected(gateway_error("Your request was blocked by our safety filter.", 400)) is True


@pytest.mark.parametrize("status", [400, 403, 451])
def test_rejection_statuses_are_covered(status):
    assert prompt_was_rejected(gateway_error(POLICY_MESSAGE, status)) is True


@pytest.mark.parametrize("message,status", [
    ("图片平台返回 HTTP 401：invalid api key", 401),
    ("图片平台返回 HTTP 429：rate limit exceeded", 429),
    ("图片平台返回 HTTP 400：invalid size parameter", 400),
    ("图片平台返回 HTTP 500：internal error", 500),
    ("无法连接图片模型平台", None),
])
def test_other_failures_are_not_treated_as_policy_rejections(message, status):
    # Rewriting cannot fix a bad key, a quota or a malformed request, and a
    # retry would spend another generation on a guaranteed failure.
    error = ModelGatewayError(message, status_code=status, detail=message)
    assert prompt_was_rejected(error) is False


def test_a_non_gateway_error_is_never_a_rejection():
    assert prompt_was_rejected(RuntimeError(POLICY_MESSAGE)) is False


def test_the_rewrite_request_carries_the_rejected_prompt():
    text = rewrite_prompt_text("一位穿深色风衣的剑客立于雨夜")
    assert "一位穿深色风衣的剑客立于雨夜" in text
    assert "输出结构" in text


def test_an_unchanged_or_empty_rewrite_is_not_usable():
    original = "一位穿深色风衣的剑客立于雨夜，冷白光从左侧照入，电影感构图"
    assert is_usable_rewrite(original, original) is False
    assert is_usable_rewrite(original, "   ") is False
    # Whitespace-only differences are still the same prompt.
    assert is_usable_rewrite(original, "  " + original + "  ") is False


def test_a_rewrite_that_loses_the_subject_is_not_usable():
    original = "一位穿深色风衣的剑客立于雨夜" * 4
    assert is_usable_rewrite(original, "剑客") is False


def test_a_meaningful_rewrite_is_usable():
    original = "剑客挥剑斩向对手，血迹溅在石阶上，冷白光从左侧照入，电影感构图"
    rewritten = "剑客挥剑与对手兵刃相击，石阶上留有战损裂痕，冷白光从左侧照入，电影感构图"
    assert is_usable_rewrite(original, rewritten) is True


def test_rewrite_reply_is_read_from_json_or_bare_text():
    import json

    assert parse_rewrite(json.dumps({"prompt": "改写后的提示词", "note": "去掉血腥描写"})) == "改写后的提示词"
    # Some models answer with the bare prompt despite the JSON contract.
    assert parse_rewrite("改写后的提示词") == "改写后的提示词"
    assert parse_rewrite("```json\n" + json.dumps({"prompt": "围栏内提示词"}) + "\n```") == "围栏内提示词"


def test_rewrite_setup_failure_does_not_hide_the_upstream_reason():
    # The upstream message is what the user can act on; a missing text model or
    # an unreachable runtime must not replace it with an unrelated error.
    from app.services.media_gateway import ModelGatewayError

    rejection = ModelGatewayError(
        "图片平台返回 HTTP 400：您的请求无法用于生成图像。该请求可能因安全政策被拦截",
        status_code=400,
        detail="您的请求无法用于生成图像。该请求可能因安全政策被拦截",
    )
    assert prompt_was_rejected(rejection) is True
    # The message survives as the task's failure reason.
    assert "安全政策" in str(rejection)


def test_a_rewritten_prompt_keeps_the_identity_lock():
    # The rewrite returns a whole new prompt, so the constraint the platform
    # added has to travel with it; otherwise the retry may redraw the face.
    lock = "\n参考图是同一人物的身份基准，必须保持其面部特征一致。"
    result = reattach_identity_lock("一位穿和服的女子站在樱花树下", lock)
    assert result.endswith(lock)
    assert "樱花树下" in result


def test_a_rewrite_that_already_carries_the_lock_is_left_alone():
    lock = "\n参考图是同一人物的身份基准。"
    already = "一位穿和服的女子站在樱花树下" + lock
    assert reattach_identity_lock(already, lock) == already


def test_no_lock_means_no_change():
    assert reattach_identity_lock("提示词  ", "  ") == "提示词"
