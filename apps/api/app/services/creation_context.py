"""Stage-scoped context and explicit screenplay/memory boundaries."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.db.models import HandbookType
from app.services.managed_skills import HANDBOOK_TASK_FILES

COMBAT_PRIORITY = (
    "【战斗动作专项优先级】仅对战斗动作编排、招式生成词、攻防连续性及大招镜头表现，"
    "系统战斗专项规则优先于导演手册、画风手册和通用精简要求。"
    "手册中的克制运镜、禁止慢镜头或简写动作等冲突约束不得削弱本次战斗设计。"
    "该覆盖不改变画风、人物外观、世界观、既有招式归属、剧情胜负和用户明确要求；"
    "实际模型能力、合法时长、参考媒体和输出结构仍是硬约束。"
)

SCRIPT_COMBAT_STAGES = frozenset({"script-generation", "script-review", "script-repair"})

def combat_stage_guidance(prompt_code: str, source: str) -> str:
    from app.services.combat_choreography import PLANNING_RULES
    planning = SCRIPT_COMBAT_STAGES | {"storyboard-generation", "storyboard-review", "storyboard-repair"}
    if prompt_code in planning:
        return COMBAT_PRIORITY + PLANNING_RULES
    if contains_combat(source):
        return (COMBAT_PRIORITY + "详细打斗由独立战斗编排模块生成，逐时间段保留攻防、运镜与光影；"
                "不得二次总结删减。用户明确的静止与固定机位仍优先。")
    return ""

def contains_combat(text: str) -> bool:
    return bool(re.search(r"战斗|打斗|搏斗|格斗|交锋|对抗|厮杀|追逐|追击|袭击|攻击|闪避|格挡|挥剑|挥刀|挥砍|突刺|劈砍|横斩|枪刺|连斩|变招|拆招|反击|大招|龙爪|剑气|对决|交手|追杀|连招|破招|终结技|法天象地|宝术|神诀|combat|fight|battle|duel", text, re.I))

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
