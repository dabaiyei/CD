from types import SimpleNamespace

import pytest

from app.services.creation_context import select_task_skills, separate_script_memory
from app.services.task_worker import GeneratedScriptPayload


def test_asset_skills_are_scoped_to_category_and_handbook():
    snapshots = [
        {"path": f"tenants/t/{kind}/h/{name}", "content": name}
        for kind in ("visual-handbooks", "director-handbooks")
        for name in (
            "README.md",
            "prefix.md",
            "character.md",
            "character-derivative.md",
            "scene.md",
            "prop.md",
            "director-planning.md",
        )
    ]
    selected = select_task_skills(
        snapshots,
        "asset-prompt-generation",
        [
            SimpleNamespace(asset_type="character", parent_asset_id=None),
        ],
    )
    assert len(selected) == 3
    assert all("visual-handbooks" in item["path"] for item in selected)
    assert {item["content"] for item in selected} == {"README.md", "prefix.md", "character.md"}
    selected = select_task_skills(
        snapshots,
        "asset-prompt-generation",
        [
            SimpleNamespace(asset_type="character", parent_asset_id="parent"),
            SimpleNamespace(asset_type="prop", parent_asset_id=None),
        ],
    )
    assert {item["content"] for item in selected} == {
        "README.md",
        "prefix.md",
        "character.md",
        "character-derivative.md",
        "prop.md",
    }


def test_memory_is_separated_without_removing_dialogue_or_actions():
    content = "场一 日 内\n角色：我的记忆状态很混乱。\n△ 他拿起书。\n△ 章节记忆：主角拿到了书。"
    result = GeneratedScriptPayload(title="本章", content=content)
    assert result.content == "场一 日 内\n角色：我的记忆状态很混乱。\n△ 他拿起书。"
    assert result.continuity_summary == "主角拿到了书。"
    assert separate_script_memory(result.content)[1] == ""
    with pytest.raises(ValueError):
        GeneratedScriptPayload(title="空剧本", content="章节记忆：仅有记忆")
