from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from runtime.config import Settings
from runtime.context import (
    RunContext,
    collect_project_file_changes,
    compose_prompt,
    legacy_state_file_path,
    prepare_run_context,
    scoped_state_file_path,
    scoped_workspace_path,
)
from runtime.contracts import AgentRunRequest, AgentRunResponse, ExecutionManifest

CINEFORGE_WORKSPACE_INSTRUCTIONS = """<workspace>
This CineForge task has an isolated {backend} workspace at {workdir}.
Use only the registered Read, Write, Edit, Glob, Grep and Skill tools.
Read Skills and relevant project files before creating the answer.
Skills, execution manifests and system-managed session data are read-only.
Existing project files may be edited only when the platform authorizes them.
Create requested text artifacts only under project-files/new.
Do not create repositories, dependency files, scratch projects or unrelated documentation.
</workspace>"""

RESPONSE_INPUT_PARSE_ATTEMPTS = 3
logger = logging.getLogger(__name__)


class _ToolCallIdNormalizingStream:
    """Fill missing tool-call IDs from loosely OpenAI-compatible streams."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self.iterator: Any = None
        self.tool_call_ids: dict[tuple[int, int], str] = {}

    async def __aenter__(self) -> _ToolCallIdNormalizingStream:
        entered = await self.source.__aenter__()
        self.iterator = entered.__aiter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        return await self.source.__aexit__(exc_type, exc, traceback)

    def __aiter__(self) -> _ToolCallIdNormalizingStream:
        return self

    async def __anext__(self) -> Any:
        chunk = await self.iterator.__anext__()
        for choice_index, choice in enumerate(getattr(chunk, "choices", None) or []):
            delta = getattr(choice, "delta", None)
            for position, tool_call in enumerate(getattr(delta, "tool_calls", None) or []):
                raw_index = getattr(tool_call, "index", None)
                index = raw_index if isinstance(raw_index, int) else position
                key = (choice_index, index)
                tool_call_id = getattr(tool_call, "id", None)
                if isinstance(tool_call_id, str) and tool_call_id:
                    self.tool_call_ids[key] = tool_call_id
                    continue
                normalized_id = self.tool_call_ids.setdefault(
                    key,
                    f"call_cineforge_{uuid4().hex}",
                )
                tool_call.id = normalized_id
        return chunk


def is_transient_response_input_parse_error(exc: Exception) -> bool:
    """Identify an upstream Responses parser failure that is safe to retry."""
    if getattr(exc, "status_code", None) != 400:
        return False
    detail = f"{exc} {getattr(exc, 'body', None)}".lower()
    return (
        "json_parse_error" in detail
        and "failed to deserialize" in detail
        and "responseinput" in detail
    )


async def retry_transient_response_input_errors(
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Retry only the known transient upstream Responses JSON parser failure."""
    for attempt in range(RESPONSE_INPUT_PARSE_ATTEMPTS):
        try:
            return await operation()
        except Exception as exc:
            if (
                not is_transient_response_input_parse_error(exc)
                or attempt + 1 >= RESPONSE_INPUT_PARSE_ATTEMPTS
            ):
                raise
            delay = float(2**attempt)
            logger.warning(
                "Responses upstream rejected a valid input payload; retrying attempt %d/%d in %.1fs",
                attempt + 2,
                RESPONSE_INPUT_PARSE_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError("unreachable Responses retry state")


async def call_responses_with_chat_fallback(
    response_operation: Callable[[], Awaitable[Any]],
    chat_operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Use Chat Completions only after the Responses parser rejects valid input."""
    try:
        return await retry_transient_response_input_errors(response_operation)
    except Exception as exc:
        if not is_transient_response_input_parse_error(exc):
            raise
        logger.warning(
            "Responses input parsing failed after %d attempts; falling back to Chat Completions",
            RESPONSE_INPUT_PARSE_ATTEMPTS,
        )
        return await chat_operation()


class RuntimeAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    async def run(
        self,
        request: AgentRunRequest,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentRunResponse: ...


class AgentScopeAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return find_spec("agentscope") is not None

    async def run(
        self,
        request: AgentRunRequest,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentRunResponse:
        if not self.available:
            raise RuntimeError("AgentScope SDK is not installed in this runtime image")

        from agentscope.agent import Agent, ReActConfig
        from agentscope.credential import OpenAICredential
        from agentscope.message import Base64Source, DataBlock, Msg, TextBlock, UserMsg
        from agentscope.model import OpenAIChatModel, OpenAIResponseModel
        from agentscope.permission import PermissionContext, PermissionMode
        from agentscope.state import AgentState
        from agentscope.tool import Edit, Glob, Grep, Read, Toolkit, Write
        from agentscope.workspace import LocalWorkspace

        class CineForgeOpenAIChatModel(OpenAIChatModel):
            async def _parse_stream_response(self, start_datetime: Any, response: Any) -> Any:
                normalized_stream = _ToolCallIdNormalizingStream(response)
                async for chunk in super()._parse_stream_response(start_datetime, normalized_stream):
                    yield chunk

        context = await asyncio.to_thread(prepare_run_context, self.settings.absolute_data_root, request)
        if request.state_mode == "persistent":
            state, resumed, receipt = await asyncio.to_thread(
                self._load_state,
                context,
                AgentState,
                PermissionContext,
                PermissionMode,
            )
        else:
            state = AgentState(
                session_id=context.state_file.stem,
                permission_context=PermissionContext(mode=PermissionMode.BYPASS),
            )
            resumed = False
            receipt = None
        if receipt and receipt.get("task_id") == request.task_id:
            return AgentRunResponse.model_validate(receipt["response"])
        parameters: dict[str, Any] = {}
        binding = request.model_binding
        if binding.max_tokens is not None:
            parameters["max_tokens"] = binding.max_tokens
        if binding.reasoning_effort in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            parameters["reasoning_effort"] = binding.reasoning_effort
            parameters["thinking_enable"] = binding.reasoning_effort != "none"

        if self._is_official_xai_binding(binding.provider, binding.model, binding.base_url):
            from agentscope.credential import XAICredential
            from agentscope.model import XAIChatModel

            host = urlparse(str(binding.base_url)).hostname or "api.x.ai"
            xai_parameters = {
                key: value
                for key, value in parameters.items()
                if key in {"max_tokens", "reasoning_effort", "thinking_enable"}
            }
            model = XAIChatModel(
                credential=XAICredential(
                    api_key=binding.api_key.get_secret_value(),
                    api_host=host,
                ),
                model=binding.model,
                parameters=XAIChatModel.Parameters(**xai_parameters),
                stream=True,
                context_size=self.settings.model_context_size,
            )
        elif binding.api_mode == "responses":
            credential = OpenAICredential(
                api_key=binding.api_key.get_secret_value(),
                base_url=str(binding.base_url).rstrip("/") if binding.base_url else None,
            )
            client_kwargs = {
                "timeout": self.settings.request_timeout_seconds,
                "default_headers": binding.extra_headers or None,
            }
            chat_fallback = CineForgeOpenAIChatModel(
                credential=credential,
                model=binding.model,
                parameters=CineForgeOpenAIChatModel.Parameters(**parameters),
                stream=True,
                context_size=self.settings.model_context_size,
                client_kwargs=client_kwargs,
            )

            class CineForgeOpenAIResponseModel(OpenAIResponseModel):
                async def _call_api(self, *args: Any, **kwargs: Any) -> Any:
                    parent = super()
                    return await call_responses_with_chat_fallback(
                        lambda: parent._call_api(*args, **kwargs),
                        lambda: chat_fallback._call_api(*args, **kwargs),
                    )

            model = CineForgeOpenAIResponseModel(
                credential=credential,
                model=binding.model,
                parameters=CineForgeOpenAIResponseModel.Parameters(**parameters),
                stream=True,
                context_size=self.settings.model_context_size,
                client_kwargs=client_kwargs,
            )
        else:
            credential = OpenAICredential(
                api_key=binding.api_key.get_secret_value(),
                base_url=str(binding.base_url).rstrip("/") if binding.base_url else None,
            )
            model = CineForgeOpenAIChatModel(
                credential=credential,
                model=binding.model,
                parameters=CineForgeOpenAIChatModel.Parameters(**parameters),
                stream=True,
                context_size=self.settings.model_context_size,
                client_kwargs={
                    "timeout": self.settings.request_timeout_seconds,
                    "default_headers": binding.extra_headers or None,
                },
            )

        events: list[dict[str, Any]] = []
        streamed_text: list[str] = []
        final_message: Msg | None = None
        async with LocalWorkspace(
            workdir=str(context.workspace),
            skill_paths=[str(path) for path in context.skill_packages],
            instructions=CINEFORGE_WORKSPACE_INSTRUCTIONS,
        ) as workspace:
            if request.tool_mode == "workspace":
                guard = self._path_guard(context)
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
                system_prompt = f"{request.system_prompt}\n\n{await workspace.get_instructions()}"
            else:
                toolkit = Toolkit()
                system_prompt = request.system_prompt
            agent = Agent(
                name="CineForgeAgent",
                system_prompt=system_prompt,
                model=model,
                toolkit=toolkit,
                state=state,
                offloader=workspace,
                react_config=ReActConfig(max_iters=self.settings.max_react_iterations),
            )
            effective_prompt = compose_prompt(
                request,
                context.workspace,
                include_conversation_context=not resumed,
            )
            user_content = [TextBlock(text=effective_prompt)]
            user_content.extend(
                DataBlock(
                    name=attachment.name,
                    source=Base64Source(data=attachment.data, media_type=attachment.mime_type),
                )
                for attachment in request.attachments
            )
            async for item in agent.reply_stream(
                UserMsg(name="User", content=user_content),
                yield_final_msg=True,
            ):
                if isinstance(item, Msg):
                    final_message = item
                else:
                    event = self._safe_event(item)
                    if event is not None:
                        events.append(event)
                    stream_event = self._stream_event(item)
                    if stream_event is not None:
                        if stream_event["type"] == "TEXT_BLOCK_DELTA":
                            streamed_text.append(str(stream_event.get("delta") or ""))
                        if on_event is not None:
                            # Live delivery is best-effort; the durable final result remains authoritative.
                            with suppress(Exception):
                                await on_event(stream_event)
            state = agent.state

        if final_message is None:
            raise RuntimeError("AgentScope did not return a final message")
        changes = await asyncio.to_thread(collect_project_file_changes, context.workspace, request)
        response = AgentRunResponse(
            session_id=state.session_id,
            final_response=final_message.get_text_content() or "".join(streamed_text),
            finish_reason=(
                final_message.finished_reason.value
                if hasattr(final_message.finished_reason, "value")
                else str(final_message.finished_reason or "completed")
            ),
            events=events,
            project_file_changes=changes,
            manifest=ExecutionManifest(
                contract_version=request.contract_version,
                provider=binding.provider,
                model=binding.model,
                prompt_versions=request.prompt_versions,
                skill_versions=request.skill_versions,
            ),
        )
        if request.state_mode == "persistent":
            await asyncio.to_thread(
                self._save_state,
                context.state_file,
                state,
                request.task_id,
                response,
            )
        return response

    @staticmethod
    def _path_guard(context: RunContext):
        from runtime.path_guard import WorkspacePathGuard

        return WorkspacePathGuard(context.workspace, context.writable_files, context.new_files_root)

    @staticmethod
    def _is_official_xai_binding(provider: str, model: str, base_url: Any) -> bool:
        """Use AgentScope's native xAI protocol only for the official host."""
        host = urlparse(str(base_url or "")).hostname or ""
        normalized_provider = provider.strip().lower()
        normalized_model = model.strip().lower()
        return host in {"api.x.ai", "api.xai.com"} and (
            normalized_provider in {"xai", "grok"} or normalized_model.startswith("grok")
        )

    @staticmethod
    def _load_state(context: RunContext, state_type, permission_type, permission_mode):
        if context.state_file.is_file() and not context.state_file.is_symlink():
            try:
                import json

                payload = json.loads(context.state_file.read_text(encoding="utf-8"))
                envelope = payload if isinstance(payload, dict) and "state" in payload else None
                state = state_type.model_validate(envelope["state"] if envelope else payload)
                if state.session_id == context.state_file.stem:
                    state.permission_context = permission_type(mode=permission_mode.BYPASS)
                    return state, True, (envelope or {}).get("last_run")
            except (OSError, ValueError):
                pass
        return (
            state_type(
                session_id=context.state_file.stem,
                permission_context=permission_type(mode=permission_mode.BYPASS),
            ),
            False,
            None,
        )

    @staticmethod
    def _save_state(
        path: Path,
        state: Any,
        task_id: str,
        response: AgentRunResponse,
    ) -> None:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        envelope = {
            "format_version": 1,
            "state": state.model_dump(mode="json"),
            "last_run": {
                "task_id": task_id,
                "response": response.model_dump(mode="json"),
            },
        }
        temporary.write_text(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)

    def delete_session_state(self, tenant_id: str, project_id: str, session_id: str) -> bool:
        data_root = self.settings.absolute_data_root
        states_root = (self.settings.absolute_data_root / "states").resolve()
        existed = False
        state_files = (
            scoped_state_file_path(data_root, tenant_id, project_id, session_id).resolve(),
            legacy_state_file_path(data_root, tenant_id, project_id, session_id).resolve(),
        )
        for state_file in state_files:
            if not state_file.is_relative_to(states_root):
                raise ValueError("invalid state path")
            if state_file.is_file() and not state_file.is_symlink():
                state_file.unlink()
                existed = True
        workspaces_root = (self.settings.absolute_data_root / "workspaces").resolve()
        workspaces = (
            scoped_workspace_path(data_root, tenant_id, project_id, session_id).resolve(),
            (data_root / "workspaces" / tenant_id / project_id / session_id).resolve(),
        )
        for workspace in workspaces:
            if (
                workspace.is_relative_to(workspaces_root)
                and workspace.is_dir()
                and not workspace.is_symlink()
            ):
                import shutil

                shutil.rmtree(workspace)
                existed = True
        return existed

    @staticmethod
    def _safe_event(event: Any) -> dict[str, Any] | None:
        payload = event.model_dump(mode="json")
        event_type = payload.get("type")
        allowed_fields = {
            "REPLY_START": {"type", "id", "created_at", "session_id", "reply_id", "name"},
            "REPLY_END": {"type", "id", "created_at", "session_id", "reply_id", "finished_reason"},
            "MODEL_CALL_START": {"type", "id", "created_at", "reply_id", "model_name"},
            "MODEL_CALL_END": {
                "type",
                "id",
                "created_at",
                "reply_id",
                "input_tokens",
                "output_tokens",
                "cache_input_tokens",
                "cache_creation_input_tokens",
                "finished_reason",
            },
            "TOOL_CALL_START": {
                "type",
                "id",
                "created_at",
                "reply_id",
                "tool_call_id",
                "tool_call_name",
            },
            "TOOL_CALL_END": {"type", "id", "created_at", "reply_id", "tool_call_id"},
            "TOOL_RESULT_START": {
                "type",
                "id",
                "created_at",
                "reply_id",
                "tool_call_id",
                "tool_call_name",
            },
            "TOOL_RESULT_END": {"type", "id", "created_at", "reply_id", "tool_call_id", "state"},
        }.get(event_type)
        if allowed_fields is None:
            return None
        return {key: value for key, value in payload.items() if key in allowed_fields}

    @classmethod
    def _stream_event(cls, event: Any) -> dict[str, Any] | None:
        payload = event.model_dump(mode="json")
        if payload.get("type") == "TEXT_BLOCK_DELTA":
            return {
                key: payload[key]
                for key in ("type", "id", "created_at", "reply_id", "block_id", "delta")
                if key in payload
            }
        if payload.get("type") == "THINKING_BLOCK_START":
            return {
                key: payload[key]
                for key in ("type", "id", "created_at", "reply_id", "block_id")
                if key in payload
            }
        return cls._safe_event(event)
