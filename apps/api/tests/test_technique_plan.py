"""The chapter technique plan: declared once at script time, reused downstream."""
from types import SimpleNamespace

import pytest

from app.services.combat_techniques import (
    TechniquePlan,
    plan_from_payload,
    plan_requirements,
)


def technique_asset(owner, short_name):
    return SimpleNamespace(asset_metadata={
        "technique_owner_name": owner,
        "combat_technique": {"name": short_name},
    })


def plan_of(*items):
    return TechniquePlan.model_validate({"techniques": list(items)})


def item(name, character, duration=2.5, **extra):
    return {"name": name, "character": character, "purpose": "解决战斗问题",
            "duration_seconds": duration, **extra}


def test_plan_rejects_duplicate_names_within_one_character():
    with pytest.raises(ValueError, match="重复声明"):
        plan_of(item("裂天斩", "剑仙"), item("裂天斩", "剑仙"))
    # The same short name under different characters is legitimate.
    plan_of(item("裂天斩", "剑仙"), item("裂天斩", "枪仙"))


@pytest.mark.parametrize("value", [None, {}, {"techniques": []}, {"unrelated": 1},
                                   {"techniques": [{"name": "缺少其他字段"}]}])
def test_plan_reading_tolerates_missing_or_malformed_stored_values(value):
    # Rows written before plans existed must not block later stages.
    assert plan_from_payload(value).techniques == []


def test_known_techniques_are_reused_and_only_new_ones_need_design():
    plan = plan_of(
        item("裂天斩", "剑仙"), item("回锋式", "剑仙", 1.5), item("龙啸", "应龙", 3.0))
    catalog = [technique_asset("剑仙", "裂天斩")]

    resolved, missing = plan_requirements(plan, catalog, {"剑仙", "应龙"})

    assert [entry.name for entry in resolved] == ["裂天斩"]
    assert [entry.name for entry in missing] == ["回锋式", "龙啸"]
    # The declared duration survives reconciliation so later stages can budget it.
    assert missing[0].duration_seconds == 1.5


def test_a_character_absent_from_the_chapter_cannot_own_a_technique():
    plan = plan_of(item("裂天斩", "剑仙"), item("龙啸", "应龙", 3.0))
    catalog = [technique_asset("剑仙", "裂天斩")]

    _, missing = plan_requirements(plan, catalog, {"剑仙"})

    assert missing == []


def test_variant_must_inherit_an_existing_technique_of_the_same_character():
    with pytest.raises(ValueError, match="但该基础招式不存在"):
        plan_requirements(
            plan_of(item("裂天斩·逆转", "剑仙", 2.0, variant_of="不存在式")),
            [technique_asset("剑仙", "裂天斩")], {"剑仙"})

    plan = plan_of(item("裂天斩·逆转", "剑仙", 2.0, variant_of="裂天斩"))
    _, missing = plan_requirements(plan, [technique_asset("剑仙", "裂天斩")], {"剑仙"})
    assert [entry.variant_of for entry in missing] == ["裂天斩"]
