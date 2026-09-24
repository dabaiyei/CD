"""Stage-scoped retrieval for the adapted shuohao storyboard method.

Mirrors martial_skill_retrieval: the catalog carries descriptions only, and a
file body is opened after routing decides it is relevant. Loading the whole pack
into every storyboard request would crowd the context window for no benefit, so
each stage declares exactly which references it may need.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).with_name("storyboard_skills")
SOURCE = "eternityspring/shuohao-skills"
SOURCE_REVISION = "7ebef4f2f53159ee1eaaec2793271a114a8be8cc"
MAX_SKILL_CHARS = 6000

# Static stage routing: these references are always relevant to the stage.
STAGE_FILES: dict[str, tuple[str, ...]] = {
    "storyboard-generation": ("cutting.md", "reference-images.md", "quality-gates.md"),
    "storyboard-repair": ("cutting.md", "reference-images.md", "quality-gates.md"),
    "storyboard-review": ("quality-gates.md",),
    "video-prompt-generation": ("h3-prompt.md",),
}

# Optional extras a stage may pull in when the request actually needs them. Each
# entry is scoped to its stage so, for example, storyboard-image reference
# discipline cannot drift into a video-prompt request that merely says 参考图.
STAGE_OPTIONAL_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    "storyboard-review": (
        ("reference-images.md", r"资产|设定图|参考图|首帧|尾帧|reference"),
    ),
}


def _read(filename: str) -> str:
    return (ROOT / filename).read_text(encoding="utf-8")


def select_files(prompt_code: str, query: str) -> list[str]:
    """Pick the references for one request: stage defaults plus content routes."""
    # Routing may only widen a stage that is already allowed to load this pack;
    # otherwise a query that merely mentions 资产 or 参考图 would pull storyboard
    # references into script, asset, chat or video stages.
    if prompt_code not in STAGE_FILES:
        return []
    selected = list(STAGE_FILES[prompt_code])
    for filename, pattern in STAGE_OPTIONAL_FILES.get(prompt_code, ()):
        if filename not in selected and re.search(pattern, query, re.I):
            selected.append(filename)
    return selected


def retrieve(prompt_code: str, query: str = "") -> tuple[str, dict]:
    """Return the inline reference block plus a manifest for cache invalidation."""
    files = select_files(prompt_code, query)
    if not files:
        return "", {}
    parts, hashes = [], {}
    for filename in files:
        content = _read(filename)
        parts.append(f'<storyboard-reference name="{filename}">\n{content}\n</storyboard-reference>')
        hashes[filename] = hashlib.sha256(content.encode()).hexdigest()
    context = "\n\n".join(parts)
    if len(context) > MAX_SKILL_CHARS:
        raise ValueError("分镜方法参考超出上下文预算，请精简对应参考文件")
    return context, {
        "source": SOURCE,
        "revision": SOURCE_REVISION,
        "selected": files,
        "hashes": hashes,
        "characters": len(context),
    }


def guidance(prompt_code: str, query: str = "") -> str:
    """The system-prompt half: rules that must apply regardless of retrieval."""
    if prompt_code not in STAGE_FILES:
        return ""
    return (
        "\n分镜方法（学习来源：eternityspring/shuohao-skills，作者烁皓，Apache-2.0，"
        "已内化并适配本项目；不依赖运行时访问 GitHub）：\n"
        "- 参考正文以内联 <storyboard-reference> 提供，按需读取，不要尝试调用外部技能或文件工具。\n"
        "- 平台注入的模型能力、合法时长、参考媒体数量与格式、输出结构是最高优先级硬约束；"
        "方法里的固定秒数示例与之冲突时映射到当前模型的合法档位，不得照抄。\n"
        "- 明确要求的静止、固定机位或一镜到底优先于本文的动感与运镜建议。\n"
        "- 战斗专项规则对招式、连续攻防和大招表现优先于本方法；画风、人物外观与胜负仍按项目设定。\n"
        "- 本方法只给分镜与提示词技法，不改剧本事实、台词原文、资产归属与结果。"
    )
