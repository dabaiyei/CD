import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.agent_runtime import AgentRuntimeRequest
from app.services.retrieval_context import automatic_request, evidence_file, mount
from app.services.script_review import requests, review_script, parse


def request():
    return automatic_request(AgentRuntimeRequest(tenant_id="t", project_id="p", task_id="task",
        session_id="s", prompt="原文和剧本", system_prompt="平台规则", model_binding={},
        prompt_versions={}, skill_versions={}, skills=[], memory_context=[]), rules="约束", references="手册")


async def noop(*args):
    pass


def test_packets_keep_complete_target_and_all_reference_bytes():
    ref = "ABCDEF" * 12000
    req = mount(request(), evidence_file("task-memory", ref))
    packets = requests(req, "完整原文与剧本")
    assert len(packets) > 1
    assert all(p.tool_mode == "none" and not p.project_files and not p.skills for p in packets)
    assert all("完整原文与剧本" in p.prompt for p in packets)
    assert all(len(p.prompt) + len(p.system_prompt) <= 48000 for p in packets)
    assert sum(p.prompt.count("ABCDEF") for p in packets) >= 11980


def test_long_target_is_not_truncated_or_silently_approved():
    req = request()
    packets = requests(req, "字" * 60000)
    assert packets[0].tool_mode == "retrieval"
    assert packets[0].project_files == req.project_files


def test_failed_packet_resume_reuses_completed_verdicts():
    req = mount(request(), evidence_file("task-memory", "资料" * 30000))
    state, calls = {}, []
    class Runtime:
        async def run(self, current):
            calls.append(current.prompt)
            if len(calls) > 1:
                raise RuntimeError("连接断开")
            return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[]}', manifest={})
    with pytest.raises(RuntimeError):
        asyncio.run(review_script(req, "原文与剧本", Runtime, state, noop, noop))
    assert len(state["completed"]) == 1
    pending = []
    class Recovered:
        async def run(self, current):
            pending.append(current.prompt)
            return SimpleNamespace(final_response='{"approved":true,"summary":"通过","findings":[]}', manifest={})
    result, _ = asyncio.run(review_script(req, "原文与剧本", Recovered, state, noop, noop))
    assert result.approved and len(pending) == len(requests(req, "原文与剧本")) - 1


def test_major_cannot_be_approved_and_empty_rejection_cannot_pass():
    assert not parse(json.dumps({"approved": True, "summary": "误报通过", "findings": [
        {"severity": "major", "location": "场1", "issue": "人物身份冲突", "suggestion": "按原文修正"}]})).approved
    with pytest.raises(ValueError):
        parse('{"approved":false,"summary":"将读取资料","findings":[]}')
