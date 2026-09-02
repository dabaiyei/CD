from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runtime import CONTRACT_VERSION, HARNESS_SDK_VERSION, RUNTIME_VERSION
from runtime.config import get_settings
from runtime.context import collect_project_file_changes, prepare_run_context
from runtime.contracts import AgentRunRequest, AgentRunResponse, ExecutionManifest
from runtime.main import app


class FakeAdapter:
    available = True

    def run(self, request: AgentRunRequest) -> AgentRunResponse:
        workspace, _home, _prompt = prepare_run_context(
            get_settings().absolute_data_root,
            request,
        )
        assert workspace.parts[-3:] == (request.tenant_id, request.project_id, request.task_id)
        return AgentRunResponse(
            session_id=request.session_id,
            final_response="story-outline",
            finish_reason="completed",
            events=[{"type": "turn/end"}],
            manifest=ExecutionManifest(
                contract_version=request.contract_version,
                provider=request.model_binding.provider,
                model=request.model_binding.model,
                prompt_versions=request.prompt_versions,
                skill_versions=request.skill_versions,
            ),
        )


def request_payload() -> dict[str, object]:
    content = "# Visual style\nUse restrained cinematic lighting."
    return {
        "contract_version": "v1",
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "task_id": "task-1",
        "session_id": "session-1",
        "prompt": "Create a story outline.",
        "system_prompt": "You are the screenplay agent.",
        "model_binding": {
            "provider": "deepseek-official",
            "model": "deepseek-chat",
            "api_key": "test-key",
        },
        "prompt_versions": {"screenplay-agent": "3"},
        "skill_versions": {"visual-handbook": "7"},
        "skills": [
            {
                "path": "visual/README.md",
                "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "version": "7",
            }
        ],
    }


def v2_request_payload() -> dict[str, object]:
    payload = request_payload()
    payload["contract_version"] = "v2"
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


def test_health_exposes_pinned_versions(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["runtime_version"] == RUNTIME_VERSION
    assert response.json()["harness_sdk_version"] == HARNESS_SDK_VERSION
    assert response.json()["contract_version"] == CONTRACT_VERSION


def test_run_requires_internal_auth(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        response = client.post("/internal/v1/runs", json=request_payload())
    assert response.status_code == 401


def test_versioned_run_contract_and_tenant_workspace(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        app.state.adapter = FakeAdapter()
        response = client.post(
            "/internal/v1/runs",
            headers={"X-Internal-Token": get_settings().internal_token},
            json=request_payload(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["final_response"] == "story-outline"
    assert body["manifest"]["harness_sdk_version"] == HARNESS_SDK_VERSION
    manifest = (
        tmp_path
        / "workspaces"
        / "tenant-1"
        / "project-1"
        / "task-1"
        / ".cineforge"
        / "execution-manifest.json"
    )
    assert manifest.is_file()


def test_v2_run_route_materializes_project_files(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    with TestClient(app) as client:
        app.state.adapter = FakeAdapter()
        response = client.post(
            "/internal/v2/runs",
            headers={"X-Internal-Token": get_settings().internal_token},
            json=v2_request_payload(),
        )
    assert response.status_code == 200
    assert response.json()["manifest"]["contract_version"] == "v2"
    project_file = (
        tmp_path
        / "workspaces"
        / "tenant-1"
        / "project-1"
        / "task-1"
        / "project-files"
        / "file-1"
        / "project-memory.md"
    )
    assert project_file.read_text(encoding="utf-8").startswith("# Project memory")


def test_project_file_changes_are_collected_as_structured_diff(tmp_path: Path) -> None:
    request = AgentRunRequest.model_validate(v2_request_payload())
    workspace, _home, _prompt = prepare_run_context(tmp_path, request)
    existing = workspace / "project-files" / "file-1" / "project-memory.md"
    existing.write_text("# Project memory\nThe lead trusts nobody.", encoding="utf-8")
    created = workspace / "project-files" / "new" / "story-outline.md"
    created.write_text("# Three-act outline", encoding="utf-8")

    changes = collect_project_file_changes(workspace, request)

    assert [(item.operation, item.file_id, item.name) for item in changes] == [
        ("update", "file-1", None),
        ("create", None, "story-outline.md"),
    ]
    assert changes[0].base_sha256 == request.project_files[0].sha256
    assert changes[1].content == "# Three-act outline"


def test_read_only_project_files_can_share_an_explicit_directory(tmp_path: Path) -> None:
    payload = v2_request_payload()
    first = payload["project_files"][0]  # type: ignore[index]
    first["editable"] = False
    first["directory_id"] = "current-chapter-context"
    first["path"] = "project-files/current-chapter-context/current-chapter.md"
    original = "# Chapter original\nOriginal prose."
    payload["project_files"].append(  # type: ignore[union-attr]
        {
            "id": "current-chapter-original",
            "directory_id": "current-chapter-context",
            "name": "chapter-original.md",
            "kind": "chapter_original",
            "path": "project-files/current-chapter-context/chapter-original.md",
            "content": original,
            "sha256": hashlib.sha256(original.encode()).hexdigest(),
            "editable": False,
        }
    )

    request = AgentRunRequest.model_validate(payload)
    workspace, _home, _prompt = prepare_run_context(tmp_path, request)

    assert (workspace / request.project_files[0].path).is_file()
    assert (workspace / request.project_files[1].path).is_file()


def test_editable_project_file_cannot_use_a_shared_directory() -> None:
    payload = v2_request_payload()
    payload["project_files"][0]["directory_id"] = "shared-context"  # type: ignore[index]
    payload["project_files"][0]["path"] = (  # type: ignore[index]
        "project-files/shared-context/project-memory.md"
    )

    with pytest.raises(ValueError, match="editable project file directory"):
        AgentRunRequest.model_validate(payload)


def test_repeated_context_preparation_removes_stale_task_snapshots(tmp_path: Path) -> None:
    request = AgentRunRequest.model_validate(v2_request_payload())
    workspace, _home, _prompt = prepare_run_context(tmp_path, request)
    stale_project_file = workspace / "project-files" / "new" / "stale-outline.md"
    stale_project_file.write_text("stale result from interrupted attempt", encoding="utf-8")
    stale_skill_file = workspace / ".cineforge" / "skills" / "stale-skill.md"
    stale_skill_file.write_text("stale skill", encoding="utf-8")

    workspace, _home, _prompt = prepare_run_context(tmp_path, request)

    assert not stale_project_file.exists()
    assert not stale_skill_file.exists()
    assert collect_project_file_changes(workspace, request) == []
    restored = workspace / "project-files" / "file-1" / "project-memory.md"
    assert restored.read_text(encoding="utf-8") == request.project_files[0].content


def test_skill_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    get_settings().data_root = tmp_path
    payload = request_payload()
    payload["skills"][0]["sha256"] = "0" * 64  # type: ignore[index]
    request = AgentRunRequest.model_validate(payload)
    try:
        prepare_run_context(tmp_path, request)
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("invalid skill hash was accepted")
