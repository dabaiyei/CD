from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class AgentRuntimeProjectFileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    directory_id: str | None = None
    name: str
    kind: str
    path: str
    content: str
    sha256: str
    editable: bool


class AgentRuntimeProjectFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    file_id: str | None = None
    name: str | None = None
    content: str | None = None
    base_sha256: str | None = None


class AgentRuntimeAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    mime_type: str
    data: str


class AgentRuntimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "v2"
    tenant_id: str
    project_id: str
    task_id: str
    session_id: str
    prompt: str
    system_prompt: str
    model_binding: dict[str, Any]
    prompt_versions: dict[str, str]
    skill_versions: dict[str, str]
    skills: list[dict[str, Any]]
    memory_context: list[str]
    state_mode: str = "persistent"
    tool_mode: str = "workspace"
    conversation_summary: str | None = None
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    project_files: list[AgentRuntimeProjectFileSnapshot] = Field(default_factory=list)
    attachments: list[AgentRuntimeAttachment] = Field(default_factory=list)


class AgentRuntimeResponse(BaseModel):
    session_id: str
    final_response: str
    finish_reason: str | None
    events: list[dict[str, Any]]
    manifest: dict[str, Any]
    project_file_changes: list[AgentRuntimeProjectFileChange] = Field(default_factory=list)


class AgentRuntimeRequestError(RuntimeError):
    def __init__(self, public_message: str, *, status_code: int) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.status_code = status_code


async def raise_for_runtime_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    await response.aread()
    status_code = response.status_code
    if status_code == 422:
        try:
            detail = response.json().get("detail", [])
        except (ValueError, AttributeError):
            detail = []
        issues = detail if isinstance(detail, list) else []
        has_project_path_issue = any(
            isinstance(issue, dict)
            and "project_files" in issue.get("loc", [])
            and "path does not match" in str(issue.get("msg", ""))
            for issue in issues
        )
        message = (
            "Agent Runtime 请求校验失败：项目文件路径与标识不一致"
            if has_project_path_issue
            else "Agent Runtime 请求校验失败，请联系管理员检查运行时契约"
        )
    elif status_code in {401, 403}:
        message = "Agent Runtime 鉴权失败，请联系管理员检查内部配置"
    elif status_code >= 500:
        try:
            runtime_detail = response.json().get("detail")
        except (ValueError, AttributeError):
            runtime_detail = None
        safe_prefixes = (
            "模型",
            "Grok",
            "无法连接模型供应商",
            "Agent Runtime 执行失败",
        )
        message = (
            runtime_detail
            if isinstance(runtime_detail, str) and runtime_detail.startswith(safe_prefixes)
            else "Agent Runtime 暂时不可用，请稍后重试"
        )
    else:
        message = "Agent Runtime 拒绝了本次请求，请联系管理员检查运行时配置"
    raise AgentRuntimeRequestError(message, status_code=status_code)


class AgentRuntimeClient:
    def __init__(self, base_url: str, internal_token: str, timeout_seconds: float = 900.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token
        self.timeout = httpx.Timeout(timeout_seconds)

    async def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(
                "/internal/v2/runs",
                headers={"X-Internal-Token": self.internal_token},
                json=request.model_dump(mode="json"),
            )
            if response.status_code in {404, 405}:
                legacy_payload = request.model_dump(
                    mode="json",
                    exclude={
                        "project_files",
                        "attachments",
                        "state_mode",
                        "tool_mode",
                        "conversation_summary",
                        "recent_messages",
                    },
                )
                legacy_payload["contract_version"] = "v1"
                response = await client.post(
                    "/internal/v1/runs",
                    headers={"X-Internal-Token": self.internal_token},
                    json=legacy_payload,
                )
            await raise_for_runtime_status(response)
            return AgentRuntimeResponse.model_validate(response.json())

    async def run_stream(
        self,
        request: AgentRuntimeRequest,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> AgentRuntimeResponse:
        async with (
            httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client,
            client.stream(
                "POST",
                "/internal/v2/runs/stream",
                headers={"X-Internal-Token": self.internal_token},
                json=request.model_dump(mode="json"),
            ) as response,
        ):
            if response.status_code in {404, 405}:
                return await self.run(request)
            await raise_for_runtime_status(response)
            async for line in response.aiter_lines():
                if not line:
                    continue
                envelope = json.loads(line)
                if envelope.get("type") == "event" and isinstance(envelope.get("event"), dict):
                    await on_event(envelope["event"])
                elif envelope.get("type") == "result":
                    return AgentRuntimeResponse.model_validate(envelope.get("result"))
                elif envelope.get("type") == "error":
                    raise RuntimeError(str(envelope.get("message") or "Agent Runtime execution failed"))
        raise RuntimeError("Agent Runtime stream ended before the final result")

    async def delete_session(self, *, tenant_id: str, project_id: str, session_id: str) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(10.0)) as client:
            response = await client.delete(
                f"/internal/v2/sessions/{tenant_id}/{project_id}/{session_id}",
                headers={"X-Internal-Token": self.internal_token},
            )
            if response.status_code == 404:
                return
            response.raise_for_status()
