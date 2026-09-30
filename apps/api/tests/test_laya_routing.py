import asyncio
import httpx
import pytest
from pydantic import SecretStr
from app.services import laya_routing as laya, personal_routing as routing
from app.core.config import get_settings
from test_personal_routing import state


def answer(value, score=.98, **extra):
    options = {"text": .005, "image": .005, "video": .005, "clarify": .005}
    options[value] = score
    return {"type": "choice", "choice": value, "probabilities": options, "confidence": .1, **extra}


def test_payload_is_small_and_does_not_leak_full_history():
    value = state("生成苹果图片")
    value["history"] = [{"role": "user", "content": "SECRET" * 10000}]
    value["models"] = [{"credentials": "SECRET"}]
    request = laya.request_for(value)
    import json
    assert "SECRET" not in json.dumps(request)
    assert len(json.dumps(request, ensure_ascii=False)) < 600
    assert all(isinstance(q["instructions"], str) for q in request["questions"].values())
    with pytest.raises(ValueError):
        laya.request_for({"message": "长" * 1201})


@pytest.mark.parametrize("score,expected", [(.94, None), (.95, "image"), (.99, "image")])
def test_gate_uses_probability_not_jev_confidence(score, expected):
    response = laya.decisions(state("画苹果"), {"answers": {"output": answer("image", score)}})
    assert routing.accepted(response["answers"], "output", {"image"}) == expected


def test_separation_and_missing_probabilities_abstain():
    raw = answer("image", .96)
    raw["probabilities"]["text"] = .6
    result = laya.decisions(state("画苹果"), {"answers": {"output": raw}})
    assert not result["answers"]["output"]["_laya_accepted"]
    raw.pop("probabilities")
    result = laya.decisions(state("画苹果"), {"answers": {"output": raw}})
    assert not result["answers"]["output"]["_laya_accepted"]


@pytest.mark.parametrize("message", ["帮我写视频提示词", "不要生成图片，先讨论一下", "以后都参考第一张图", "生成视频失败了吗，查一下进度"])
def test_explicit_text_beats_wrong_media_prediction(message):
    result = laya.decisions(state(message), {"answers": {"output": answer("image")}})
    assert routing.accepted(result["answers"], "output", {"text", "image"}) == "text"


@pytest.mark.parametrize("message,expected", [
    ("写提示词并生成一张雨中的城市图片", "image"),
    ("不要生成图片了，改为生成一个海浪视频", "video"),
    ("不是让你写故事，是让你生成一张狐狸图片", "image"),
])
def test_explicit_generation_overrides_earlier_text_or_negation(message, expected):
    result = laya.decisions(state(message), {"answers": {"output": answer(expected)}})
    assert routing.accepted(result["answers"], "output", {"image", "video"}) == expected


def test_cancel_is_not_overridden_or_sent_back_to_jev():
    value = state("取消任务，不要继续生成")
    value["media_plan"] = {"status": "ready", "index": 0, "steps": [{"type": "image"}]}
    result = laya.decisions(value, {"answers": {"output": answer("image"),
        "plan_action": {"type": "choice", "choice": "execute", "probabilities": {"execute": .999}}}})
    assert laya.complete_for(value, result)
    merged = routing.merge_decision(value, result)
    assert merged["output"] == "text"
    assert merged["media_plan"]["status"] == "cancelled"


def test_model_cannot_grant_auto_execution_or_rewrite():
    raw = {"answers": {key: {"type": "choice", "choice": value, "probabilities": {value: .999, "other": .001}}
        for key, value in [("chain", "auto"), ("rewrite", "yes"), ("plan_action", "execute")]}}
    result = laya.decisions(state("我想了解这个功能"), raw)
    assert all(not result["answers"][key]["_laya_accepted"] for key in raw["answers"])


@pytest.mark.parametrize("failure", ["low", "timeout"])
def test_existing_jev_fallback(monkeypatch, failure):
    settings = get_settings()
    monkeypatch.setattr(settings, "laya_routing_enabled", True)
    monkeypatch.setattr(settings, "laya_base_url", "http://local.test")
    monkeypatch.setattr(settings, "laya_api_key", SecretStr("local-test"))
    async def local(*args, **kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout")
        return {"answers": {}}
    monkeypatch.setattr(laya, "evaluate", local)
    calls = []
    async def post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"model": "jev-fallback", "answers": {}}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = asyncio.run(routing.evaluate(state("画苹果")))
    assert result["model"] == "jev-fallback"
    assert len(calls) == 1
