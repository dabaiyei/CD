from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from runtime import AGENTSCOPE_VERSION, CONTRACT_VERSION, RUNTIME_VERSION
from runtime.config import get_settings
from runtime.context import (
    collect_project_file_changes,
    legacy_state_file_path,
    prepare_run_context,
    scoped_state_file_path,
    scoped_workspace_path,
)
from runtime.contracts import AgentRunRequest, AgentRunResponse, ExecutionManifest
from runtime.main import app


class FakeAdapter:
    available = True

    async def run(self, request: AgentRunRequest, on_event=None) -> AgentRunResponse:
        context = prepare_run_context(get_settings().absolute_data_root, request)
        workspace_key = request.session_id if request.state_mode == "persistent" else request.task_id
        assert context.workspace == scoped_workspace_path(
            get_settings().absolute_data_root,
            request.tenant_id,
            request.project_id,
            workspace_key,
        ).resolve()
        if on_event is not None:
            await on_event({"type": "THINKING_BLOCK_START", "block_id": "private-thinking"})
            await on_event({"type": "TEXT_BLOCK_DELTA", "delta": "story-"})
            await on_event({"type": "TEXT_BLOCK_DELTA", "delta": "outline"})
        return AgentRunResponse(
            session_id=request.session_id,
            final_response="story-outline",
            finish_reason="completed",
            events=[{"type": "REPLY_END"}],
            manifest=ExecutionManifest(
                contract_version=request.contract_version,
                provider=request.model_binding.provider,
                model=request.model_binding.model,
                prompt_versions=request.prompt_versions,
                skill_versions=request.skill_versions,
            ),
        )


class FailingAdapter:
    available = True

    async def run(self, request: AgentRunRequest, on_event=None) -> AgentRunResponse:
        error = RuntimeError("sensitive-upstream-response")
        error.status_code = 400  # type: ignore[attr-defined]
        raise error


def request_payload() -> dict[str, object]:
    content = "# Visual style\nUse restrained cinematic lighting."
    return {
        "contract_version": "v2",
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "task_id": "task-1",
        "session_id": "session-1",
        "prompt": "Create a story outline.",
        "system_prompt": "You are the screenplay agent.",
        "model_binding": {
            "provider": "openai-compatible",
            "model": "deepseek-chat",
            "api_key": "test-key",
        },
        "prompt_versions": {"screenplay-agent": "3"},
        "skill_versions": {"visual/noir": "7"},
        "skills": [
            {
                "path": "visual/noir/README.md",
                "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "version": "7",
            }
        ],
    }


def project_file_payload() -> dict[str, object]:
    payload = request_payload()
    content = "# Project memory\nThe lead distrusts the producer."
    payload["project_files"] = [
        {
            "id": "file-1",
            "name": "project-memory.md",
            "kind": "memory",
            "path": "project-files/file-1/project-memory.md",
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "editable": True,
        }
    ]
    return payload


def attachment_payload() -> dict[str, object]:
    payload = request_payload()
    payload["attachments"] = [
        {
            "id": "attachment-1",
            "name": "shot-reference.webp",
            "mime_type": "image/webp",
            "data": "UklGRg==",
        }
    ]
    return payload


def test_health_exposes_pinned_versions(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["runtime_type"] == "agentscope"
    assert response.json()["runtime_version"] == RUNTIME_VERSION
    assert response.json()["agentscope_version"] == AGENTSCOPE_VERSION
    assert response.json()["contract_version"] == CONTRACT_VERSION


def test_run_requires_internal_auth(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        response = client.post("/internal/v2/runs", json=request_payload())
    assert response.status_code == 401


def test_stream_requires_internal_auth(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        response = client.post("/internal/v2/runs/stream", json=request_payload())
    assert response.status_code == 401


def test_stream_emits_safe_events_and_durable_result(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        app.state.adapter = FakeAdapter()
        response = client.post(
            "/internal/v2/runs/stream",
            headers={"X-Internal-Token": get_settings().internal_token},
            json=request_payload(),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    envelopes: list[dict[str, Any]] = [json.loads(line) for line in response.text.splitlines()]
    assert [item["type"] for item in envelopes] == ["event", "event", "event", "result"]
    assert "private reasoning" not in response.text
    assert (
        "".join(item["event"].get("delta", "") for item in envelopes if item["type"] == "event")
        == "story-outline"
    )
    assert envelopes[-1]["result"]["final_response"] == "story-outline"


def test_stream_logs_runtime_failure_and_returns_sanitized_error(
    tmp_path: Path,
    caplog,
) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        app.state.adapter = FailingAdapter()
        response = client.post(
            "/internal/v2/runs/stream",
            headers={"X-Internal-Token": get_settings().internal_token},
            json=request_payload(),
        )

    envelope = json.loads(response.text)
    assert envelope == {
        "type": "error",
        "message": "模型与 OpenAI Chat Completions/工具调用格式不兼容 (HTTP 400)",
    }
    assert "task_id=task-1" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "test-key" not in caplog.text


def test_v2_contract_materializes_agentscope_skill(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        app.state.adapter = FakeAdapter()
        response = client.post(
            "/internal/v2/runs",
            headers={"X-Internal-Token": get_settings().internal_token},
            json=request_payload(),
        )
    assert response.status_code == 200
    assert response.json()["manifest"]["runtime_type"] == "agentscope"
    skill_md = next((tmp_path / "workspaces").rglob("SKILL.md"))
    content = skill_md.read_text(encoding="utf-8")
    assert "name: cineforge-visual-noir" in content
    assert "resources/README.md" in content


def test_personal_skill_only_exposes_catalog_summary_until_selected(tmp_path: Path) -> None:
    payload = request_payload()
    skill_content = (
        "# 人物近身打斗分镜技法\n\n"
        "> 检索说明：按空间关系和攻防节拍拆分近身动作镜头。\n\n"
        "> 适用阶段：分镜生成\n\n"
        "## 技能规则\n\n"
        "每个镜头只保留一个主要攻防动作，并保持动作轴连续。\n"
    )
    payload["skill_versions"] = {"user-skills/skill-1": "3"}
    payload["skills"] = [
        {
            "path": "user-skills/skill-1/README.md",
            "content": skill_content,
            "sha256": hashlib.sha256(skill_content.encode()).hexdigest(),
            "version": "3",
        }
    ]

    request = AgentRunRequest.model_validate(payload)
    context = prepare_run_context(tmp_path, request)
    skill_md = context.skill_packages[0].joinpath("SKILL.md").read_text(encoding="utf-8")
    resource = context.skill_packages[0].joinpath("resources", "README.md").read_text(
        encoding="utf-8"
    )

    assert "name: cineforge-user-skill-1" in skill_md
    assert "按空间关系和攻防节拍拆分近身动作镜头" in skill_md
    assert "每个镜头只保留一个主要攻防动作" not in skill_md
    assert "每个镜头只保留一个主要攻防动作" in resource


def test_project_files_and_structured_diff_are_preserved(tmp_path: Path) -> None:
    request = AgentRunRequest.model_validate(project_file_payload())
    context = prepare_run_context(tmp_path, request)
    existing = context.workspace / "project-files" / "file-1" / "project-memory.md"
    existing.write_text("# Project memory\nThe lead trusts nobody.", encoding="utf-8")
    created = context.new_files_root / "story-outline.md"
    created.write_text("# Three-act outline", encoding="utf-8")

    changes = collect_project_file_changes(context.workspace, request)

    assert [(item.operation, item.file_id, item.name) for item in changes] == [
        ("update", "file-1", None),
        ("create", None, "story-outline.md"),
    ]
    assert changes[0].base_sha256 == request.project_files[0].sha256


def test_v2_contract_accepts_image_attachments() -> None:
    request = AgentRunRequest.model_validate(attachment_payload())

    assert request.attachments[0].name == "shot-reference.webp"
    assert request.attachments[0].mime_type == "image/webp"


def test_read_only_project_files_can_share_an_explicit_directory(tmp_path: Path) -> None:
    payload = request_payload()
    contents = ["# Chapter context", "# Chapter original\nOriginal prose."]
    payload["project_files"] = [
        {
            "id": "current-chapter-context",
            "directory_id": "current-chapter-context",
            "name": "current-chapter.md",
            "kind": "chapter_context",
            "path": "project-files/current-chapter-context/current-chapter.md",
            "content": contents[0],
            "sha256": hashlib.sha256(contents[0].encode()).hexdigest(),
            "editable": False,
        },
        {
            "id": "current-chapter-original",
            "directory_id": "current-chapter-context",
            "name": "chapter-original.md",
            "kind": "chapter_original",
            "path": "project-files/current-chapter-context/chapter-original.md",
            "content": contents[1],
            "sha256": hashlib.sha256(contents[1].encode()).hexdigest(),
            "editable": False,
        },
    ]

    request = AgentRunRequest.model_validate(payload)
    context = prepare_run_context(tmp_path, request)

    assert (context.workspace / request.project_files[0].path).is_file()
    assert (context.workspace / request.project_files[1].path).is_file()


def test_editable_project_file_cannot_use_a_shared_directory() -> None:
    payload = project_file_payload()
    payload["project_files"][0]["directory_id"] = "shared-context"  # type: ignore[index]
    payload["project_files"][0]["path"] = (  # type: ignore[index]
        "project-files/shared-context/project-memory.md"
    )

    with pytest.raises(ValueError, match="editable project file directory"):
        AgentRunRequest.model_validate(payload)


def test_repeated_preparation_removes_stale_attempt_files(tmp_path: Path) -> None:
    request = AgentRunRequest.model_validate(project_file_payload())
    context = prepare_run_context(tmp_path, request)
    stale = context.new_files_root / "stale.md"
    stale.write_text("stale", encoding="utf-8")

    context = prepare_run_context(tmp_path, request)

    assert not stale.exists()
    assert collect_project_file_changes(context.workspace, request) == []


def test_skill_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = request_payload()
    payload["skills"][0]["sha256"] = "0" * 64  # type: ignore[index]
    request = AgentRunRequest.model_validate(payload)
    try:
        prepare_run_context(tmp_path, request)
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("invalid skill hash was accepted")


def test_long_realistic_skill_scope_uses_compact_workspace_path(tmp_path: Path) -> None:
    payload = request_payload()
    scope_id = "9d716da5-f49d-4c1e-859c-3a06e24995ac"
    payload.update(
        tenant_id="11c71fbd-5422-4a24-a1ae-e50a75dd6889",
        project_id="720ebefb-0b0d-412f-8dae-7814b89fe879",
        task_id="d8b18f0b-6074-4ddb-b61d-649a390c803e",
        session_id="d8b18f0b-6074-4ddb-b61d-649a390c803e",
    )
    payload["skills"][0]["path"] = (  # type: ignore[index]
        f"tenants/{payload['tenant_id']}/visual-handbooks/{scope_id}/"
        "technique-storyboard-table-design.md"
    )
    payload["skill_versions"] = {
        f"tenants/{payload['tenant_id']}/visual-handbooks/{scope_id}": "1"
    }
    request = AgentRunRequest.model_validate(payload)

    context = prepare_run_context(tmp_path, request)

    assert len(context.workspace.name) == 32
    assert next(context.workspace.rglob("technique-storyboard-table-design.md")).is_file()


def test_existing_session_state_is_migrated_to_compact_scope(tmp_path: Path) -> None:
    request = AgentRunRequest.model_validate(request_payload())
    legacy = legacy_state_file_path(tmp_path, request.tenant_id, request.project_id, request.session_id)
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"state":"preserved"}', encoding="utf-8")

    context = prepare_run_context(tmp_path, request)

    assert context.state_file == scoped_state_file_path(
        tmp_path, request.tenant_id, request.project_id, request.session_id
    ).resolve()
    assert context.state_file.read_text(encoding="utf-8") == '{"state":"preserved"}'
    assert not legacy.exists()
