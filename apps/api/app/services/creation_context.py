"""Stage-scoped context and explicit screenplay/memory boundaries."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.db.models import HandbookType
from app.services.managed_skills import HANDBOOK_TASK_FILES

SCRIPT_OUTPUT_BOUNDARY = (
    "输出隔离规则：content 只能包含剧本场次、动作和对白。"
    "人物状态、事件总结、伏笔清单和章节记忆只能放在独立 continuity_summary 字段，"
    "由平台写入记忆文件；禁止在剧本正文追加‘章节记忆’‘记忆状态’或内部执行说明。"
    "review_notes 单独记录审核说明，也不得混进 content。"
)


def select_task_skills(snapshots: list[dict], prompt_code: str, assets=()) -> list[dict]:
    required = {kind: set(names) for kind, names in HANDBOOK_TASK_FILES.get(prompt_code, {}).items()}
    if prompt_code == "asset-prompt-generation":
        visual = {"README.md", "prefix.md"}
        for asset in assets:
            kind = str(asset.asset_type)
            if kind in {"character", "scene", "prop"}:
                visual.add(f"{kind}.md")
                if asset.parent_asset_id:
                    visual.add(f"{kind}-derivative.md")
        required = {HandbookType.VISUAL: visual}
    selected, seen = [], set()
    for snapshot in snapshots:
        path = PurePosixPath(str(snapshot["path"]).replace("\\", "/"))
        kind = (
            HandbookType.VISUAL
            if "visual-handbooks" in path.parts
            else HandbookType.DIRECTOR
            if "director-handbooks" in path.parts
            else None
        )
        if "user-skills" not in path.parts and path.name not in required.get(kind, set()):
            continue
        # A file's identity includes its package; identically named README files differ.
        if str(path) not in seen:
            selected.append(snapshot)
            seen.add(str(path))
    return selected


def separate_script_memory(content: str) -> tuple[str, str]:
    """Extract only explicitly labelled internal metadata, never dialogue about memory."""
    body, memory = [], []
    marker = re.compile(
        r"^\s*(?:[△•*-]\s*)?(?:\*\*)?(?:章节记忆|连续性记忆|记忆状态)(?:\*\*)?\s*[:：]\s*(.+)$"
    )
    for line in content.splitlines():
        match = marker.match(line)
        if match:
            memory.append(match.group(1))
        else:
            body.append(line)
    return "\n".join(body).strip(), "\n".join(memory)
