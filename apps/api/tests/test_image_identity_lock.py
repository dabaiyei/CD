"""An uploaded photo is the person to restyle, so the face must survive the edit."""
import pytest

from app.services.task_worker import image_identity_lock, keeps_reference_identity


def test_the_lock_forbids_redrawing_the_face_but_allows_styling_changes():
    lock = image_identity_lock(character=False, keep_identity=True)
    # The face is fixed...
    for phrase in ("脸型", "五官形状", "眼距", "鼻型", "唇形", "眉形"):
        assert phrase in lock
    assert "不得生成另一张脸" in lock
    # ...while the things users actually ask to change stay editable.
    for phrase in ("妆容", "发型", "服装", "光线"):
        assert phrase in lock


def test_the_character_variant_keeps_its_existing_wording():
    lock = image_identity_lock(character=True, keep_identity=True)
    assert "面容" in lock and "不得生成另一个人物" in lock


def test_an_explicit_replacement_request_suppresses_the_lock():
    # Injecting "keep this face" here would contradict the user outright.
    for instruction in ("把这张图里的人换成另一个人", "换脸成动漫风格", "改成别人", "不要用这张脸",
                        "replace the person with someone else", "change the face"):
        assert keeps_reference_identity(instruction) is False, instruction
        lock = image_identity_lock(character=False, keep_identity=keeps_reference_identity(instruction))
        assert lock == ""


@pytest.mark.parametrize("instruction", [
    "帮我换个造型妆容",
    "把头发改成短发",
    "改一下妆，换成浓一点的",
    "换一身衣服",
    "把背景换成夜晚",
    "让她笑一下",
    "调整一下光线",
    "换个人设风格的妆",
    "重新生成一版，皮肤再亮一点",
    "",
])
def test_styling_and_editing_requests_keep_the_identity_lock(instruction):
    # These mention changes, but never ask for a different person, which is what
    # the original bug looked like: a makeup edit that redrew the face.
    assert keeps_reference_identity(instruction) is True, instruction


def test_the_platform_lock_is_not_left_to_the_model_to_write():
    # The bug was the personal path relying on the model to mention identity;
    # the lock is now appended by the platform whenever a reference is attached.
    assert image_identity_lock(character=False, keep_identity=True).strip()
