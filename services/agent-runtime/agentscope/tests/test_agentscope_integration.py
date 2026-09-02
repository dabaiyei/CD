from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agentscope.agent import Agent
from agentscope.event import (
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockStartEvent,
)
from agentscope.message import AssistantMsg, DataBlock
from agentscope.tool import Edit, Glob, Grep, Read, Toolkit, Write
from agentscope.workspace import LocalWorkspace
from test_contract import attachment_payload, project_file_payload

from runtime.adapter import AgentScopeAdapter
from runtime.config import Settings
from runtime.context import prepare_run_context, scoped_state_file_path
from runtime.contracts import AgentRunRequest
from runtime.path_guard import WorkspacePathGuard


def test_real_agentscope_workspace_loads_skills_and_only_safe_tools(tmp_path: Path) -> None:
    async def verify() -> None:
        request = AgentRunRequest.model_validate(project_file_payload())
        context = prepare_run_context(tmp_path, request)
        guard = WorkspacePathGuard(
            context.workspace,
            context.writable_files,
            context.new_files_root,
        )
        async with LocalWorkspace(
            workdir=str(context.workspace),
            skill_paths=[str(path) for path in context.skill_packages],
        ) as workspace:
            backend = workspace.get_backend()
            tools = [
                Read(backend=backend, middlewares=[guard]),
                Write(backend=backend, middlewares=[guard]),
                Edit(backend=backend, middlewares=[guard]),
                Glob(backend=backend, middlewares=[guard]),
                Grep(backend=backend, middlewares=[guard]),
            ]
            toolkit = Toolkit(
                tools=tools,
                skills_or_loaders=await workspace.list_skills(agent_id="cineforge-agent"),
            )
            instructions = await toolkit.get_skill_instructions()
            skill_definition = context.skill_packages[0] / "SKILL.md"

        assert {tool.name for tool in tools} == {"Read", "Write", "Edit", "Glob", "Grep"}
        assert instructions is not None
        assert "cineforge-visual-noir" in instructions
        assert "必须使用" in instructions
        assert "必须先使用 Read 工具读取 resources/README.md" in skill_definition.read_text(
            encoding="utf-8"
        )

    asyncio.run(verify())


def test_real_agentscope_read_tool_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    async def verify() -> None:
        request = AgentRunRequest.model_validate(project_file_payload())
        context = prepare_run_context(tmp_path / "runtime", request)
        guard = WorkspacePathGuard(
            context.workspace,
            context.writable_files,
            context.new_files_root,
        )
        tool = Read(middlewares=[guard])
        result = await tool(file_path=str(tmp_path / "outside.txt"))
        with pytest.raises(PermissionError, match="outside the task workspace"):
            async for _ in result:
                pass

    asyncio.run(verify())


def test_adapter_executes_real_agentscope_stack_without_exposing_thinking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_reply_stream(
        self: Agent,
        inputs,
        structured_schema=None,
        yield_final_msg: bool = False,
    ):
        del inputs, structured_schema, yield_final_msg
        reply_id = "reply-1"
        yield ReplyStartEvent(
            session_id=self.state.session_id,
            reply_id=reply_id,
            name=self.name,
        )
        yield ThinkingBlockDeltaEvent(
            reply_id=reply_id,
            block_id="thinking-1",
            delta="private reasoning",
        )
        yield ThinkingBlockStartEvent(reply_id=reply_id, block_id="thinking-2")
        yield TextBlockDeltaEvent(
            reply_id=reply_id,
            block_id="text-1",
            delta="AgentScope integration result",
        )
        yield ReplyEndEvent(session_id=self.state.session_id, reply_id=reply_id)
        yield AssistantMsg(name=self.name, content="AgentScope integration result")

    monkeypatch.setattr(Agent, "reply_stream", fake_reply_stream)
    settings = Settings(data_root=tmp_path)
    request = AgentRunRequest.model_validate(project_file_payload())

    streamed_events: list[dict[str, object]] = []

    async def collect_event(event: dict[str, object]) -> None:
        streamed_events.append(event)

    async def run() -> object:
        return await AgentScopeAdapter(settings).run(request, collect_event)

    result = asyncio.run(run())

    assert result.final_response == "AgentScope integration result"
    assert result.manifest.runtime_type == "agentscope"
    assert [event["type"] for event in result.events] == ["REPLY_START", "REPLY_END"]
    assert [event["type"] for event in streamed_events] == [
        "REPLY_START",
        "THINKING_BLOCK_START",
        "TEXT_BLOCK_DELTA",
        "REPLY_END",
    ]
    assert streamed_events[2]["delta"] == "AgentScope integration result"
    assert "private reasoning" not in str(streamed_events)
    assert scoped_state_file_path(tmp_path, "tenant-1", "project-1", "session-1").is_file()


def test_adapter_passes_image_attachment_as_multimodal_user_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_reply_stream(
        self: Agent,
        inputs,
        structured_schema=None,
        yield_final_msg: bool = False,
    ):
        del self, structured_schema, yield_final_msg
        captured["inputs"] = inputs
        yield AssistantMsg(name="CineForgeAgent", content="image received")

    monkeypatch.setattr(Agent, "reply_stream", fake_reply_stream)
    request = AgentRunRequest.model_validate(attachment_payload())
    result = asyncio.run(AgentScopeAdapter(Settings(data_root=tmp_path)).run(request))

    user_message = captured["inputs"]
    assert isinstance(user_message.content[1], DataBlock)
    assert user_message.content[1].source.media_type == "image/webp"
    assert result.final_response == "image received"


def test_adapter_reuses_stable_state_and_returns_same_task_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    prompts: list[str] = []

    async def fake_reply_stream(
        self: Agent,
        inputs,
        structured_schema=None,
        yield_final_msg: bool = False,
    ):
        del self, structured_schema, yield_final_msg
        calls.append("run")
        prompts.append(inputs.get_text_content())
        yield AssistantMsg(name="CineForgeAgent", content=f"reply-{len(calls)}")

    monkeypatch.setattr(Agent, "reply_stream", fake_reply_stream)
    request = AgentRunRequest.model_validate(
        {
            **project_file_payload(),
            "memory_context": ["[story/hero] 主角害怕失去记忆。"],
            "conversation_summary": "主角正在调查旧港口。",
            "recent_messages": [{"role": "user", "content": "先确认调查目标"}],
        }
    )
    adapter = AgentScopeAdapter(Settings(data_root=tmp_path))

    first = asyncio.run(adapter.run(request))
    replay = asyncio.run(adapter.run(request))
    next_turn = asyncio.run(
        adapter.run(
            request.model_copy(
                update={"task_id": "task-2", "prompt": "继续设计第二幕冲突"}
            )
        )
    )

    assert first.final_response == replay.final_response == "reply-1"
    assert next_turn.final_response == "reply-2"
    assert calls == ["run", "run"]
    assert "Platform-managed long-term memory" in prompts[0]
    assert "Platform-managed conversation summary for state recovery" in prompts[0]
    assert "Recent authoritative conversation for state recovery" in prompts[0]
    assert "Platform-managed long-term memory" in prompts[1]
    assert "Platform-managed conversation summary for state recovery" not in prompts[1]
    assert adapter.delete_session_state("tenant-1", "project-1", "session-1") is True
    assert not scoped_state_file_path(tmp_path, "tenant-1", "project-1", "session-1").exists()


def test_adapter_ephemeral_run_ignores_existing_sdk_state_and_uses_platform_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def fake_reply_stream(
        self: Agent,
        inputs,
        structured_schema=None,
        yield_final_msg: bool = False,
    ):
        del self, structured_schema, yield_final_msg
        prompts.append(inputs.get_text_content())
        yield AssistantMsg(name="CineForgeAgent", content=f"reply-{len(prompts)}")

    monkeypatch.setattr(Agent, "reply_stream", fake_reply_stream)
    base = AgentRunRequest.model_validate(
        {
            **project_file_payload(),
            "conversation_summary": "旧港口调查已经完成。",
            "recent_messages": [{"role": "user", "content": "下一步检查仓库"}],
        }
    )
    adapter = AgentScopeAdapter(Settings(data_root=tmp_path))

    asyncio.run(adapter.run(base))
    state_file = scoped_state_file_path(tmp_path, "tenant-1", "project-1", "session-1")
    state_before = state_file.read_text(encoding="utf-8")
    ephemeral = base.model_copy(
        update={"task_id": "task-2", "prompt": "继续", "state_mode": "ephemeral"}
    )
    result = asyncio.run(adapter.run(ephemeral))

    assert result.final_response == "reply-2"
    assert "Platform-managed conversation summary for state recovery" in prompts[1]
    assert "Recent authoritative conversation for state recovery" in prompts[1]
    assert state_file.read_text(encoding="utf-8") == state_before
