import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import project_jev as module
from app.services.jev_configuration import JevSettings

pytestmark = pytest.mark.usefixtures('jev_disabled')


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    module._cache.clear()
    module._cooldowns.clear()

    async def config(db, tenant):
        return JevSettings(True, "test-key", route_confidence=0.65)

    monkeypatch.setattr(module, "get_jev_configuration", config)


def answers(**values):
    return {
        "answers": {
            key: {"type": "choice", "choice": value, "confidence": 0.99} for key, value in values.items()
        }
    }


def test_cached_decisions_are_tenant_and_evidence_scoped(monkeypatch):
    calls = []

    async def call(config, stage, evidence):
        calls.append(evidence)
        return answers(combat="exchange", performance="mixed")

    monkeypatch.setattr(module, "call", call)

    async def run():
        checkpoint = {}
        first = await module.decide("a", "video-prompt-generation", {"action": "交锋"}, checkpoint=checkpoint)
        assert first["decisions"]["combat"] == "exchange"
        await module.decide("a", "video-prompt-generation", {"action": "交锋"}, checkpoint=checkpoint)
        await module.decide("b", "video-prompt-generation", {"action": "交锋"})
        await module.decide("a", "video-prompt-generation", {"action": "行走"})
        assert len(calls) == 3
        module._cache.clear()
        await module.decide("a", "video-prompt-generation", {"action": "交锋"}, checkpoint=checkpoint)
        assert len(calls) == 3

    asyncio.run(run())


def test_provider_failure_cooldown_never_stops_generation(monkeypatch):
    calls = []

    async def call(*args):
        calls.append(1)
        raise httpx.ReadTimeout("no response")

    monkeypatch.setattr(module, "call", call)

    async def run():
        first = await module.decide("a", "script-generation", {"text": "故事"})
        second = await module.decide("a", "storyboard-generation", {"text": "场景"})
        assert first["status"] == second["status"] == "fallback"
        assert len(calls) == 1

    asyncio.run(run())


def test_low_confidence_invalid_choice_and_secret_redaction(monkeypatch):
    async def call(config, stage, evidence):
        assert evidence == {"action": "静止"}
        return {
            "answers": {
                "combat": {"type": "choice", "choice": "exchange", "confidence": 0.6},
                "repair_scope": {"type": "choice", "choice": "rewrite_all", "confidence": 1},
                "timing": {"type": "choice", "choice": "suspect", "confidence": 0.99},
            }
        }

    monkeypatch.setattr(module, "call", call)
    result = asyncio.run(
        module.decide(
            "a", "storyboard-repair", {"action": "静止", "api_key": "secret", "media_url": "private"}
        )
    )
    assert result["decisions"] == {"timing": "suspect"}
    assert "时长与语速" in module.guidance(result)
    assert "不是通过结论" in module.guidance(result)


def test_oversized_evidence_never_sent_or_used_to_approve(monkeypatch):
    async def forbidden(*args):
        raise AssertionError("must not send full chapter")

    monkeypatch.setattr(module, "call", forbidden)
    result = asyncio.run(module.decide("a", "storyboard-review", {"script": "文" * 20000}))
    assert result["reason"] == "evidence_budget"
    assert not module.guidance(result)


def test_disabled_control_review_does_not_repeat_legacy_triage(monkeypatch):
    from test_storyboard_review import noop, request, shots

    from app.services.storyboard_review import review_board

    async def call(*args):
        raise AssertionError('legacy triage must not run')

    monkeypatch.setattr(module, "call", call)
    calls = []

    class Runtime:
        async def run(self, req):
            calls.append(req)
            return SimpleNamespace(
                final_response=json.dumps(
                    {
                        "approved": False,
                        "summary": "动作矛盾",
                        "findings": [
                            {
                                "severity": "major",
                                "shot_indices": [1],
                                "fields": ["action_description"],
                                "issue": "站立与坐下同时发生",
                                "suggestion": "写清过渡",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                manifest={},
            )

    verdict, _ = asyncio.run(
        review_board(request(), Runtime, shots=shots(1), script="", state={}, save=noop, progress=noop)
    )
    assert not verdict.approved
    assert verdict.findings
    assert len(calls) == 1
    assert "JEV 局部任务辅助判断" not in calls[0].system_prompt


def test_enrichment_keeps_model_tools_and_files(monkeypatch):
    from test_storyboard_review import request

    async def call(*args):
        return answers(operation="continue", performance="speech")

    monkeypatch.setattr(module, "call", call)
    original = request()
    updated = asyncio.run(module.enrich(original, "script-generation", {"text": "续写"}))
    assert updated.model_binding == original.model_binding
    assert updated.tool_mode == original.tool_mode
    assert updated.project_files == original.project_files
    assert updated.prompt == original.prompt
    assert "承接已有内容" in updated.system_prompt
