"""Bounded, stage-specific retrieval for the adapted CY martial arts skill."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).with_name("combat_skills")
SOURCE_REVISION = "65c7c1dd35fcf4a01bfc1372ab506b60e2d0d26d"
METHOD_SOURCE_REVISION = "f0626a01d32a476c4e827b3fa1aba6dcdf571ead"
MAX_SKILL_CHARS = 4800
MAX_HANDBOOK_CHARS = 9000
MAX_REQUEST_CHARS = 32000

# The catalog contains descriptions only. File bodies are opened after routing.
CATALOG = {
    "close-quarters": ("徒手/桥手/肘膝密集近战", r"徒手|拳|咏春|八极|肘|膝|近身|wing chun|boxing|melee"),
    "grappling": ("摔投/缠斗/地面控制", r"摔|缠抱|缠斗|柔术|柔道|擒拿|mma|grappl|wrestl"),
    "sword": ("刀剑连续变线与格挡回弹", r"剑|刀|斩|劈|刃|sword|blade|saber|katana"),
    "polearm": ("长枪/棍的距离与换把", r"长枪|银枪|枪尖|枪杆|枪仙|棍|矛|戟|spear|polearm|staff"),
    "flexible": ("鞭/链/双截棍的张力回收", r"鞭|锁链|双截棍|三节棍|whip|chain|nunchaku"),
    "spell": ("宝术/神通/招式特效方向", r"法术|法印|法相|神通|宝术|神诀|召唤|剑气|大招|spell|magic|summon"),
    "giant": ("巨物与小体型人物的空间关系", r"巨龙|苍龙|神龙|巨兽|巨型|巨物|法天象地|dragon|coloss|giant"),
    "chase": ("高速位移、追击与地形互动", r"追击|追逐|追杀|逃脱|高速移动|高速跃|狂奔|疾驰|chase|pursuit|sprint"),
}
CATALOG_GUIDANCE = (
    "\n武指按需目录：combat_plan.windows[].martial_systems 可选最多2个最相关ID："
    + "；".join(f"{key}={value[0]}" for key, value in CATALOG.items())
    + "。只选择本段实际体系，不为了使用技能新增动作；详细文件在视频阶段读取。\n"
)

# Independent scene methods: metadata only; no upstream document is mounted.
SCENE_METHODS = {
    "restricted-space": r"狭窄|窄巷|走廊|水下|失重|斜坡|独木桥|摇晃|坍塌|悬空|narrow|underwater|zero.gravity",
    "multi-opponent": r"围攻|围杀|一对多|以一敌|多人夹击|夹攻|包围|surrounded|outnumbered",
    "asymmetric": r"体型差|巨龙|苍龙|巨兽|巨型|重甲|破绽|弱点|以弱胜强|以小搏大|铠甲|armou?r|giant|dragon",
}


def match_score(pattern: str, query: str) -> int:
    # Repeating a weapon name throughout asset metadata must not crowd out other systems.
    return len({match.group(0).lower() for match in re.finditer(pattern, query, re.I)})


def shot_query(row: dict) -> str:
    # Never classify from general prompts or the catalog itself: they mention every discipline.
    return json.dumps({key: row.get(key) for key in (
        "title", "scene_description", "action_description", "combat_plan", "assets", "reference_map"
    )}, ensure_ascii=False)


def retrieve(row: dict) -> tuple[str, dict]:
    query = shot_query(row)
    windows = (row.get("combat_plan") or {}).get("windows", [])
    explicit = list(dict.fromkeys(key for window in windows for key in window.get("martial_systems", [])))
    if any(key not in CATALOG for key in explicit):
        raise ValueError("未知武指技能分类")
    if len(explicit) > 4:
        raise ValueError("单镜武指体系过多，请拆分镜头，每镜最多四种体系")
    if windows and all(w.get("balance") == "showdown" for w in windows):
        selected = []
    else:
        ranked = sorted(CATALOG, key=lambda key: -match_score(CATALOG[key][1], query))
        selected = (explicit + [key for key in ranked if key not in explicit
                    and re.search(CATALOG[key][1], query, re.I)])[:4]
    # Route space/tactics from the actual shot, not background asset descriptions.
    scene_query = json.dumps({key: row.get(key) for key in (
        "scene_description", "action_description", "combat_plan"
    )}, ensure_ascii=False)
    methods = []
    if not windows or any(w.get("balance") != "showdown" for w in windows):
        for key, pattern in SCENE_METHODS.items():
            if key == "asymmetric" and windows and not any(
                w.get("balance") in ("balanced", "reversal", "escape") for w in windows
            ):
                continue
            if re.search(pattern, scene_query, re.I):
                methods.append(key)
    files = ["core.md", "camera.md", *(f"{key}.md" for key in selected),
             *(f"{key}.md" for key in methods[:2])]
    parts, hashes = [], {}
    for filename in files:
        content = (ROOT / filename).read_text(encoding="utf-8")
        parts.append(f"<martial-reference name=\"{filename}\">\n{content}\n</martial-reference>")
        hashes[filename] = hashlib.sha256(content.encode()).hexdigest()
    context = "\n\n".join(parts)
    if len(context) > MAX_SKILL_CHARS:
        raise ValueError("武指参考超出上下文预算，请精简对应参考文件")
    return context, {"source": "CY-CHENYUE/martial-arts-director-cy", "revision": SOURCE_REVISION,
                     "method_source": "qualsenWeb/fight-video-create-skill",
                     "method_revision": METHOD_SOURCE_REVISION,
                     "selected": files, "hashes": hashes, "characters": len(context)}


def terms(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z-]{2,}", text.lower()))
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(word[i:i + 2] for i in range(len(word) - 1))
    return tokens - {"人物", "动作", "镜头", "参考", "保持", "可以", "生成", "提示", "要求", "一个"}


def excerpt(content: str, query: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    # Retrieve whole paragraphs; split oversized paragraphs at sentence boundaries, never mid-rule.
    blocks = []
    for paragraph in re.split(r"\n\s*\n", content):
        blocks.extend(re.split(r"(?<=[。；.!?])\s*|\n", paragraph) if len(paragraph) > limit else [paragraph])
    query_terms = terms(query)
    candidates = sorted(range(len(blocks)), key=lambda i: (i != 0, -len(terms(blocks[i]) & query_terms), i))
    chosen, used = [], 0
    for index in candidates:
        block = blocks[index].strip()
        if block and used + len(block) + 2 <= limit - 30:
            chosen.append(index)
            used += len(block) + 2
    return "[按当前镜头检索的原文片段]\n" + "\n\n".join(blocks[i] for i in sorted(chosen))


def select_personal(skills, query: str):
    query_terms = terms(query)
    ranked = sorted(skills, key=lambda s: -len(terms(s.name + " " + s.description[:220]) & query_terms))
    return [s for s in ranked if terms(s.name + " " + s.description[:220]) & query_terms][:2]


def bound_handbooks(snapshots: list[dict], query: str) -> list[dict]:
    selected, remaining = [], MAX_HANDBOOK_CHARS
    for snapshot in snapshots:
        if remaining < 200:
            break
        content = excerpt(snapshot["content"], query, min(1600, remaining))
        selected.append({**snapshot, "content": content,
                         "sha256": hashlib.sha256(content.encode()).hexdigest()})
        remaining -= len(content)
    return selected
