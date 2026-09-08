from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from runtime.contracts import AgentRunRequest, ProjectFileChange, SkillSnapshot


class InvalidSkillSnapshot(ValueError):
    pass


class InvalidProjectFileSnapshot(ValueError):
    pass


@dataclass(frozen=True)
class RunContext:
    workspace: Path
    state_file: Path
    skill_packages: list[Path]
    prompt: str
    writable_files: frozenset[Path]
    new_files_root: Path


def _scope_digest(*parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def scoped_workspace_path(data_root: Path, tenant_id: str, project_id: str, key: str) -> Path:
    return data_root / "workspaces" / _scope_digest(tenant_id, project_id, key)


def scoped_state_file_path(data_root: Path, tenant_id: str, project_id: str, session_id: str) -> Path:
    return data_root / "states" / _scope_digest(tenant_id, project_id) / f"{session_id}.json"


def legacy_state_file_path(data_root: Path, tenant_id: str, project_id: str, session_id: str) -> Path:
    return data_root / "states" / tenant_id / project_id / f"{session_id}.json"


def prepare_run_context(data_root: Path, request: AgentRunRequest) -> RunContext:
    workspace_key = request.session_id if request.state_mode == "persistent" else request.task_id
    workspace = scoped_workspace_path(
        data_root,
        request.tenant_id,
        request.project_id,
        workspace_key,
    )
    state_file = scoped_state_file_path(
        data_root,
        request.tenant_id,
        request.project_id,
        request.session_id,
    )
    snapshot_root = workspace / ".cineforge" / "skills"
    agentscope_skills_root = workspace / ".cineforge" / "agentscope-skills"
    project_files_root = workspace / "project-files"
    for root in (snapshot_root, agentscope_skills_root, project_files_root):
        if root.is_symlink():
            root.unlink()
        elif root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)

    for snapshot in request.skills:
        _verify_skill(snapshot)
        target = (
            snapshot_root
            / hashlib.sha256(snapshot.path.encode("utf-8")).hexdigest()[:16]
            / Path(snapshot.path).name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot.content, encoding="utf-8", newline="\n")

    skill_packages = _materialize_agentscope_skills(agentscope_skills_root, request)
    writable_files: set[Path] = set()
    project_manifest_files: list[dict[str, str | bool]] = []
    for snapshot in request.project_files:
        digest = hashlib.sha256(snapshot.content.encode("utf-8")).hexdigest()
        if digest != snapshot.sha256:
            raise InvalidProjectFileSnapshot(f"project file content hash mismatch: {snapshot.id}")
        target = workspace.joinpath(*snapshot.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot.content, encoding="utf-8", newline="\n")
        if snapshot.editable:
            writable_files.add(target.resolve())
        project_manifest_files.append(
            {
                "id": snapshot.id,
                "name": snapshot.name,
                "path": snapshot.path,
                "sha256": digest,
                "editable": snapshot.editable,
            }
        )

    new_files_root = project_files_root / "new"
    new_files_root.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_state_file = legacy_state_file_path(
        data_root,
        request.tenant_id,
        request.project_id,
        request.session_id,
    )
    if (
        not state_file.exists()
        and legacy_state_file.is_file()
        and not legacy_state_file.is_symlink()
    ):
        os.replace(legacy_state_file, state_file)
    manifest = {
        "contractVersion": request.contract_version,
        "runtimeType": "agentscope",
        "tenantId": request.tenant_id,
        "projectId": request.project_id,
        "taskId": request.task_id,
        "sessionId": request.session_id,
        "promptVersions": request.prompt_versions,
        "skillVersions": request.skill_versions,
        "skills": [
            {"path": item.path, "sha256": item.sha256, "version": item.version}
            for item in request.skills
        ],
        "projectFiles": project_manifest_files,
    }
    (workspace / ".cineforge" / "execution-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return RunContext(
        workspace=workspace.resolve(),
        state_file=state_file.resolve(),
        skill_packages=skill_packages,
        prompt=compose_prompt(request, workspace.resolve()),
        writable_files=frozenset(writable_files),
        new_files_root=new_files_root.resolve(),
    )


def _verify_skill(snapshot: SkillSnapshot) -> None:
    digest = hashlib.sha256(snapshot.content.encode("utf-8")).hexdigest()
    if digest != snapshot.sha256:
        raise InvalidSkillSnapshot(f"skill content hash mismatch: {snapshot.path}")


def _skill_root(snapshot: SkillSnapshot, roots: list[str]) -> str:
    matches = [root for root in roots if snapshot.path == root or snapshot.path.startswith(root + "/")]
    if matches:
        return max(matches, key=len)
    parts = snapshot.path.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else parts[0]


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.") or "cineforge-skill"


def _personal_skill_metadata(snapshots: list[SkillSnapshot]) -> tuple[str, str]:
    readme = next((item.content for item in snapshots if item.path.endswith("/README.md")), "")
    title_match = re.search(r"^#\s+(.+?)\s*$", readme, re.M)
    excerpt_match = re.search(r"^>\s*检索说明：(.+?)\s*$", readme, re.M)
    title = title_match.group(1).strip() if title_match else "个人创作技能"
    excerpt = excerpt_match.group(1).strip() if excerpt_match else "用户补充的个人创作规则"
    return title[:120], excerpt[:500]


def _materialize_agentscope_skills(root: Path, request: AgentRunRequest) -> list[Path]:
    declared_roots = [key.strip("/") for key in request.skill_versions if key.strip("/")]
    grouped: dict[str, list[SkillSnapshot]] = {}
    for snapshot in request.skills:
        grouped.setdefault(_skill_root(snapshot, declared_roots), []).append(snapshot)

    packages: list[Path] = []
    for index, (source_root, snapshots) in enumerate(sorted(grouped.items())):
        package_digest = hashlib.sha256(source_root.encode("utf-8")).hexdigest()[:12]
        package = root / f"{index:03d}-{package_digest}"
        package.mkdir(parents=True, exist_ok=True)
        resource_names: list[str] = []
        for snapshot in snapshots:
            try:
                relative = Path(snapshot.path).relative_to(source_root)
            except ValueError:
                relative = Path(snapshot.path)
            if str(relative) in {"", "."}:
                relative = Path(Path(snapshot.path).name)
            target = package / "resources" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(snapshot.content, encoding="utf-8", newline="\n")
            resource_names.append((Path("resources") / relative).as_posix())

        source_parts = source_root.split("/")
        is_personal_skill = "user-skills" in source_parts
        if "visual-handbooks" in source_parts:
            handbook_kind = "visual"
            handbook_label = "项目所选视觉手册"
        elif "director-handbooks" in source_parts:
            handbook_kind = "director"
            handbook_label = "项目所选导演手册"
        else:
            handbook_kind = None
            handbook_label = "项目创作手册"
        package_id = source_parts[-1]
        display_name = source_root.replace("/", "-")
        personal_title, personal_excerpt = _personal_skill_metadata(snapshots)
        if is_personal_skill:
            skill_name = f"cineforge-user-{_slug(package_id)}"
            description = f"用户个人 Skill《{personal_title}》：{personal_excerpt}"
        else:
            skill_name = (
                f"cineforge-{handbook_kind}-{_slug(package_id)}"
                if handbook_kind
                else f"cineforge-{_slug(display_name)}"
            )
            description = (
                f"CineForge {handbook_label}的冻结快照。"
                "当任务涉及剧本、资产、分镜、视频提示词或项目风格时必须使用。"
            )
        resources = "\n".join(f"- `{name}`" for name in resource_names)
        skill_md = (
            "---\n"
            f"name: {skill_name}\n"
            f"description: {json.dumps(description, ensure_ascii=False)}\n"
            "---\n\n"
            f"# {personal_title if is_personal_skill else display_name}\n\n"
            f"这是平台在本次任务中冻结的{'用户个人' if is_personal_skill else '项目'}技能快照。调用本技能后，"
            "必须先使用 Read 工具读取 resources/README.md 以及系统提示中列出的任务相关资源，"
            "再严格按照文件中的规则完成创作。不得用模型常识替代文件内容，也不要修改本技能目录。\n\n"
            f"## 资源文件\n\n{resources}\n"
        )
        (package / "SKILL.md").write_text(skill_md, encoding="utf-8", newline="\n")
        packages.append(package.resolve())
    return packages


def collect_project_file_changes(workspace: Path, request: AgentRunRequest) -> list[ProjectFileChange]:
    changes: list[ProjectFileChange] = []
    for snapshot in request.project_files:
        target = workspace.joinpath(*snapshot.path.split("/"))
        if not target.is_file() or target.is_symlink():
            changes.append(
                ProjectFileChange(operation="delete", file_id=snapshot.id, base_sha256=snapshot.sha256)
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


def compose_prompt(
    request: AgentRunRequest,
    workspace: Path,
    *,
    include_conversation_context: bool = True,
) -> str:
    sections = ["Platform context:"]
    if request.tool_mode == "workspace":
        sections.extend(
            [
                f"- The task workspace is {workspace}.",
                "- Authoritative skill snapshots are exposed as AgentScope skills and are read-only.",
                "- Their checksums and versions are in .cineforge/execution-manifest.json.",
            ]
        )
    else:
        sections.append(
            "- Required authoritative skill content is already embedded in the task as read-only context."
        )
    if request.project_files:
        sections.extend(
            [
                "- Project text files are under project-files/<file-id>/.",
                "- Read every relevant file before editing it.",
                "- Edit an existing file in place only when its manifest entry is editable.",
                "- Create new text files under project-files/new/ only.",
            ]
        )
    if request.memory_context:
        sections.extend(["", "Platform-managed long-term memory:", *request.memory_context])
    if include_conversation_context and request.conversation_summary:
        sections.extend(
            ["", "Platform-managed conversation summary for state recovery:", request.conversation_summary]
        )
    if include_conversation_context and request.recent_messages:
        sections.extend(["", "Recent authoritative conversation for state recovery:"])
        sections.extend(
            f"{'User' if item.role == 'user' else 'Assistant'}: {item.content}"
            for item in request.recent_messages
        )
    sections.extend(["", "Task:", request.prompt])
    return "\n".join(sections)
