from types import SimpleNamespace

from app.services.shot_continuity import neighboring_shots


def test_neighbors_follow_full_storyboard_order_with_bounded_context():
    rows = [
        SimpleNamespace(
            id=str(i), order_index=i, title=str(i), scene_description="s" * 900, action_description="a" * 9000
        )
        for i in (5, 1, 3)
    ]
    result = neighboring_shots(rows)
    assert result["3"]["previous"]["shot_order_index"] == 1
    assert result["3"]["next"]["shot_order_index"] == 5
    assert result["1"]["previous"] is None
    assert result["5"]["next"] is None
    assert len(result["3"]["previous"]["action_description"]) <= 1200
