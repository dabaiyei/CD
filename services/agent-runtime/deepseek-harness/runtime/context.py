from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from runtime.contracts import AgentRunRequest, ProjectFileChange


class InvalidSkillSnapshot(ValueError):
    pass


class InvalidProjectFileSnapshot(ValueError):
    pass


def prepare_run_context(data_root: Path, request: AgentRunRequest) -> tuple[Path, Path, str]:
    workspace = data_root / "workspaces" / request.tenant_id / request.project_id / request.task_id
    harness_home = data_root / "homes" / request.tenant_id / request.project_id
    skills_root = workspace / ".cineforge" / "skills"
    project_files_root = workspace / "project-files"
    for task_snapshot_root in (skills_root, project_files_root):
        if task_snapshot_root.is_symlink():
            task_snapshot_root.unlink()
        elif task_snapshot_root.exists():
            shutil.rmtree(task_snapshot_root)
    skills_root.mkdir(parents=True, exist_ok=True)
    project_files_root.mkdir(parents=True, exist_ok=True)
    harness_home.mkdir(parents=True, exist_ok=True)

    manifest_files: list[dict[str, str]] = []
    for snapshot in request.skills:
        digest = hashlib.sha256(snapshot.content.encode("utf-8")).hexdigest()
        if digest != snapshot.sha256:
            raise InvalidSkillSnapshot(f"skill content hash mismatch: {snapshot.path}")
        target = skills_root.joinpath(*snapshot.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot.content, encoding="utf-8", newline="\n")
        manifest_files.append({"path": snapshot.path, "sha256": digest, "version": snapshot.version})

    project_manifest_files: list[dict[str, str | bool]] = []
    for snapshot in request.project_files:
        digest = hashlib.sha256(snapshot.content.encode("utf-8")).hexdigest()
        if digest != snapshot.sha256:
            raise InvalidProjectFileSnapshot(f"project file content hash mismatch: {snapshot.id}")
        target = workspace.joinpath(*snapshot.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot.content, encoding="utf-8", newline="\n")
        project_manifest_files.append(
            {
                "id": snapshot.id,
                "name": snapshot.name,
                "path": snapshot.path,
                "sha256": digest,
                "editable": snapshot.editable,
            }
        )
    (project_files_root / "new").mkdir(parents=True, exist_ok=True)

    manifest = {
        "contractVersion": request.contract_version,
        "tenantId": request.tenant_id,
        "projectId": request.project_id,
        "taskId": request.task_id,
        "promptVersions": request.prompt_versions,
        "skillVersions": request.skill_versions,
        "files": manifest_files,
        "projectFiles": project_manifest_files,
    }
    manifest_path = workspace / ".cineforge" / "execution-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    prompt = _compose_prompt(request)
    return workspace, harness_home, prompt


def collect_project_file_changes(workspace: Path, request: AgentRunRequest) -> list[ProjectFileChange]:
    changes: list[ProjectFileChange] = []
    for snapshot in request.project_files:
        target = workspace.joinpath(*snapshot.path.split("/"))
        if not target.is_file() or target.is_symlink():
            changes.append(
                ProjectFileChange(
                    operation="delete",
                    file_id=snapshot.id,
                    base_sha256=snapshot.sha256,
                )
            )
            continue
        content = target.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != snapshot.sha256:
            changes.append(
                ProjectFileChange(
                    operation="update",
                    file_id=snapshot.id,
                    content=content,
                    base_sha256=snapshot.sha256,
                )
            )

    new_root = workspace / "project-files" / "new"
    if new_root.is_dir():
        for target in sorted(new_root.rglob("*")):
            if target.is_symlink() or not target.is_file():
                continue
            relative = target.relative_to(new_root).as_posix()
            if not relative.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml")):
                continue
            changes.append(
                ProjectFileChange(
                    operation="create",
                    name=relative,
                    content=target.read_text(encoding="utf-8"),
                )
            )
    return changes


def _compose_prompt(request: AgentRunRequest) -> str:
    sections = [
        "Platform context:",
        "- Authoritative skill snapshots are under .cineforge/skills/.",
        "- Their checksums and versions are in .cineforge/execution-manifest.json.",
        "- Treat these files as read-only instructions for this run.",
    ]
    if request.project_files:
        sections.extend(
            [
                "- Project text files are under project-files/<file-id>/.",
                "- You may read every project file. Edit an existing file in place only when its "
                "manifest entry is editable.",
                "- Create new project files directly under project-files/new/ using .md, .txt, .json, "
                ".yaml or .yml.",
                "- Do not rename existing paths or write project files anywhere else.",
            ]
        )
    if request.memory_context:
        sections.extend(["", "Platform-managed long-term memory:", *request.memory_context])
    sections.extend(["", "Task:", request.prompt])
    return "\n".join(sections)
