from __future__ import annotations

from importlib.util import find_spec
from typing import Protocol

from runtime.config import Settings
from runtime.context import collect_project_file_changes, prepare_run_context
from runtime.contracts import AgentRunRequest, AgentRunResponse, ExecutionManifest


class RuntimeAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def run(self, request: AgentRunRequest) -> AgentRunResponse: ...


class DeepSeekHarnessAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return find_spec("deepseek_harness") is not None

    def run(self, request: AgentRunRequest) -> AgentRunResponse:
        if not self.available:
            raise RuntimeError("DeepSeek Harness SDK is not installed in this runtime image")

        from deepseek_harness import DeepSeekHarness

        workspace, harness_home, prompt = prepare_run_context(
            self.settings.absolute_data_root,
            request,
        )
        binding = request.model_binding
        with DeepSeekHarness(
            provider=binding.provider,
            model=binding.model,
            reasoning_effort=binding.reasoning_effort,
            max_tokens=binding.max_tokens,
            cwd=str(workspace),
            runtime_cwd=str(workspace),
            dsh_home=str(harness_home),
            profile=self.settings.profile,
            env={"DSH_SYSTEM_PROMPT": request.system_prompt},
            base_url=str(binding.base_url) if binding.base_url else None,
            api_key=binding.api_key.get_secret_value(),
            initialize_timeout_seconds=self.settings.initialize_timeout_seconds,
            request_timeout_seconds=self.settings.request_timeout_seconds,
        ) as harness:
            result = harness.run(prompt, session_id=request.session_id)

        return AgentRunResponse(
            session_id=result.session_id,
            final_response=result.final_response,
            finish_reason=result.finish_reason,
            events=result.events,
            project_file_changes=collect_project_file_changes(workspace, request),
            manifest=ExecutionManifest(
                contract_version=request.contract_version,
                provider=binding.provider,
                model=binding.model,
                prompt_versions=request.prompt_versions,
                skill_versions=request.skill_versions,
            ),
        )
