from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import martial_skill_retrieval as retrieval
from app.services.combat_choreography import CombatDesign, PLANNING_RULES
from test_combat_choreography import design_payload


def test_sword_spear_routing_reads_only_matching_files(monkeypatch):
    read = []
    original = Path.read_text

    def tracked(path, *args, **kwargs):
        read.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    content, manifest = retrieval.retrieve({"action_description": "剑仙侧移斩击，枪仙转杆格挡"})
    assert set(read) == {"core.md", "camera.md", "sword.md", "polearm.md"}
    assert manifest["selected"] == read
    assert "摔投与缠斗" not in content
    assert "海报提示词" not in content
    assert len(content) <= retrieval.MAX_SKILL_CHARS
    assert "martial-reference" not in PLANNING_RULES
    assert "martial_systems" in PLANNING_RULES


def test_showdown_does_not_load_attack_dictionaries():
    _, manifest = retrieval.retrieve({"action_description": "剑仙和枪仙持兵器静立",
        "combat_plan": {"windows": [{"balance": "showdown", "martial_systems": ["sword"]}]}})
    assert manifest["selected"] == ["core.md", "camera.md"]


def test_scene_methods_are_conditional_and_do_not_read_unselected_files(monkeypatch):
    read = []
    original = Path.read_text

    def tracked(path, *args, **kwargs):
        read.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    _, manifest = retrieval.retrieve({"action_description": "狭窄走廊内被围攻",
        "combat_plan": {"windows": [{"balance": "balanced"}]}})
    assert "restricted-space.md" in read and "multi-opponent.md" in read
    assert "asymmetric.md" not in read
    assert read == manifest["selected"]
    assert manifest["characters"] <= retrieval.MAX_SKILL_CHARS
    read.clear()
    _, manifest = retrieval.retrieve({"action_description": "两者观察对方",
        "assets": [{"description": "曾在狭窄通道被围攻"}]})
    assert read == ["core.md", "camera.md"]


def test_asymmetry_does_not_force_comeback_in_overwhelming_window():
    row = {"action_description": "苍龙与剑仙交锋",
           "combat_plan": {"windows": [{"balance": "balanced"}]}}
    assert "asymmetric.md" in retrieval.retrieve(row)[1]["selected"]
    row["combat_plan"]["windows"][0]["balance"] = "overwhelming"
    assert "asymmetric.md" not in retrieval.retrieve(row)[1]["selected"]
    row["combat_plan"]["windows"][0]["balance"] = "showdown"
    assert retrieval.retrieve(row)[1]["selected"] == ["core.md", "camera.md"]


def test_repeated_terms_do_not_inflate_routing_and_worst_case_fits_budget():
    assert retrieval.match_score(r"剑|刀", "剑" * 100 + "刀") == 2
    _, manifest = retrieval.retrieve({
        "action_description": "狭窄走廊内围攻巨龙，法術，剑斩长枪拳鞭追击",
        "combat_plan": {"windows": [
            {"balance": "balanced", "martial_systems": ["giant", "spell"]},
            {"balance": "balanced", "martial_systems": ["sword", "polearm"]}]}})
    assert len(manifest["selected"]) == 8
    assert manifest["characters"] <= retrieval.MAX_SKILL_CHARS


def test_explicit_ai_selection_and_fantasy_are_bounded():
    _, manifest = retrieval.retrieve({"action_description": "巨龙追击，法术与剑气，长枪，拳，鞭，摔投",
        "combat_plan": {"windows": [{"balance": "balanced", "martial_systems": ["giant", "spell"]}]}})
    assert manifest["selected"][2:4] == ["giant.md", "spell.md"]
    assert len(manifest["selected"]) <= 8  # core/camera + four systems + two scene methods
    assert manifest["characters"] <= retrieval.MAX_SKILL_CHARS
    with pytest.raises(ValueError, match="未知"):
        retrieval.retrieve({"combat_plan": {"windows": [{"martial_systems": ["../../secret"]}]}})


def test_large_handbook_retrieves_relevant_late_paragraph_without_cutting_rule():
    relevant = "刀剑交锋必须保持双方持械手与反弹轨迹连贯，不能复位。"
    full = "# 手册\n\n" + "\n\n".join(f"第{i}条：校园场景使用明亮的配色。" for i in range(300)) + "\n\n" + relevant
    selected = retrieval.bound_handbooks([{"path": "visual-handbooks/test/storyboard-video.md", "content": full}], "刀剑交锋反弹轨迹")
    assert relevant in selected[0]["content"]
    assert len(selected[0]["content"]) <= 1600
    assert selected[0]["sha256"]


def test_personal_skill_selection_uses_catalog_not_every_body():
    candidates = [SimpleNamespace(name="料理", description="食材与菜谱" * 10000),
                  SimpleNamespace(name="剑术追击", description="刀剑高速交锋和剑刃变线"),
                  SimpleNamespace(name="广告文案", description="产品卖点和广告投放")]
    assert retrieval.select_personal(candidates, "刀剑高速交锋，连续变线") == [candidates[1]]


def test_fast_combat_rejects_sparse_and_long_static_beats():
    data = design_payload()
    data["beats"] = [{**data["beats"][0], "end": 5,
                      "actions": [a for beat in data["beats"] for a in beat["actions"]]}]
    with pytest.raises(ValueError, match="80%"):
        CombatDesign.model_validate(data).validate_timeline(5)
    sparse = design_payload()
    for beat in sparse["beats"]:
        beat["actions"] = beat["actions"][:1]
    with pytest.raises(ValueError, match="连续攻防"):
        CombatDesign.model_validate(sparse).validate_timeline(5)
    repeated = design_payload()
    for beat in repeated["beats"]:
        beat["actions"] = ["两人互相攻击格挡"] * 10
    with pytest.raises(ValueError, match="连续攻防"):
        CombatDesign.model_validate(repeated).validate_timeline(5)


def test_explicit_measured_tempo_keeps_approved_plan():
    data = design_payload()
    data["plan"]["windows"][0]["tempo"] = "measured"
    data["beats"] = [{**data["beats"][0], "end": 5}]
    design = CombatDesign.model_validate(deepcopy(data))
    design.validate_timeline(5, design.plan)


def test_context_changes_invalidate_cache_and_budget_blocks_provider(monkeypatch):
    import asyncio
    import json
    from contextlib import asynccontextmanager
    from app.services import combat_choreography as combat, task_worker as worker
    from app.services.agent_runtime import AgentRuntimeRequest

    task = SimpleNamespace(request_payload={})
    state = {"context": "已选择手册v1", "calls": 0}

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def owned(*args):
        return task

    async def progress(*args):
        pass

    async def request(*args, **kwargs):
        return AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task", session_id="task",
            prompt=kwargs["prompt"], system_prompt=state["context"], model_binding={},
            prompt_versions={}, skill_versions={}, skills=[], memory_context=[])

    class Runtime:
        async def run(self, request):
            state["calls"] += 1
            return SimpleNamespace(final_response=json.dumps(design_payload()), manifest={})

    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "owned_task_for_update", owned)
    monkeypatch.setattr(worker, "owns_running_task", lambda _: True)
    monkeypatch.setattr(worker, "record_progress", progress)
    monkeypatch.setattr(worker, "runtime_request", request)
    row = {"shot_id": "shot", "order_index": 1, "duration_seconds": 5,
           "action_description": "双方挥剑连续交锋", "combat_plan": design_payload()["plan"]}
    first = asyncio.run(combat.generate("task", row, {}, {}, Runtime))
    again = asyncio.run(combat.generate("task", row, {}, {}, Runtime))
    assert state["calls"] == 1
    assert first == again
    state["context"] = "已选择手册v2：新的运镜要求"
    changed = asyncio.run(combat.generate("task", row, {}, {}, Runtime))
    assert state["calls"] == 2
    assert changed[1]["fingerprint"] != first[1]["fingerprint"]
    # An oversized request must be refused before reaching the provider. Size the
    # padding from the configured budget so this keeps testing the guard rather
    # than a stale literal.
    from app.services.martial_skill_retrieval import MAX_REQUEST_CHARS

    state["context"] = "超长手册" * (MAX_REQUEST_CHARS // 2)
    with pytest.raises(RuntimeError, match=f"{MAX_REQUEST_CHARS:,}"):
        asyncio.run(combat.generate("task", row, {}, {}, Runtime))
    assert state["calls"] == 2
