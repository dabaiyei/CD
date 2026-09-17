import json

import pytest

from app.services.prompt_batches import video_prompt_batches


def test_large_storyboard_is_bounded_without_losing_continuity():
    rows = [{"shot_id": str(i), "order_index": i, "action": "动作" * 4000,
             "neighboring_shots": {"previous": i - 1, "next": i + 1}} for i in range(28)]
    batches = video_prompt_batches(rows)
    assert len(batches) > 1
    assert [row for batch in batches for row in batch] == rows
    assert all(len(json.dumps(batch, ensure_ascii=False)) <= 24000 and len(batch) <= 4 for batch in batches)


def test_oversized_single_shot_is_not_silently_truncated():
    with pytest.raises(RuntimeError, match="镜头 7"):
        video_prompt_batches([{"order_index": 7, "action": "字" * 24000}])
