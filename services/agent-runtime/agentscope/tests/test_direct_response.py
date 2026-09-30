import asyncio
from types import SimpleNamespace as NS

import pytest
from runtime import direct_response
from runtime.contracts import AgentRunRequest
from runtime.response_stream import IncompleteModelResponse
from test_response_stream import Stream


def request():
    return AgentRunRequest(tenant_id="t", project_id="p", task_id="task", session_id="s",
        prompt="只审核本镜", system_prompt="返回JSON", state_mode="ephemeral", tool_mode="none",
        model_binding={"provider": "relay", "model": "test", "api_key": "test",
                       "api_mode": "responses", "reasoning_effort": "none"},
        prompt_versions={}, skill_versions={}, skills=[], memory_context=[])


@pytest.mark.parametrize("has_text", [True, False])
def test_direct_transport_recovers_final_text_or_fails_once(monkeypatch, has_text):
    source = Stream([NS(type="response.completed", response=NS(output=[
        NS(type="message", content=[NS(type="output_text", text='{"approved":true}')])
    ] if has_text else [NS(type="reasoning")]))])
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.responses = self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def create(self, **kwargs):
            calls.append(kwargs)
            return source
    monkeypatch.setattr(direct_response, "AsyncOpenAI", Client)
    if has_text:
        response = asyncio.run(direct_response.run(request(), 300))
        assert response.final_response == '{"approved":true}'
        assert response.project_file_changes == []
    else:
        with pytest.raises(IncompleteModelResponse, match="未返回正文"):
            asyncio.run(direct_response.run(request(), 300))
    assert len(calls) == 1
    assert calls[0]["reasoning"] == {"effort": "none"}
    assert source.closed


def test_direct_path_never_bypasses_tools_or_persistent_context():
    req = request()
    assert direct_response.eligible(req)
    assert not direct_response.eligible(req.model_copy(update={"tool_mode": "retrieval"}))
    assert not direct_response.eligible(req.model_copy(update={"state_mode": "persistent"}))
    assert not direct_response.eligible(req.model_copy(update={"memory_context": ["历史事实"]}))
