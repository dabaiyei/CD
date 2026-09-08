from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.db.models import Handbook, HandbookType, PromptTemplate

ALLOWED_SKILL_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml"}


@dataclass(frozen=True)
class ManagedSkillFile:
    key: str
    filename: str
    label: str
    purpose: str


VISUAL_HANDBOOK_FILES = (
    ManagedSkillFile("readme", "README.md", "README", "画风总说明与全局使用边界"),
    ManagedSkillFile("prefix", "prefix.md", "前缀", "所有视觉生成任务共享的美学基础"),
    ManagedSkillFile("character", "character.md", "角色", "人物基础形象的生成约束"),
    ManagedSkillFile(
        "character-derivative",
        "character-derivative.md",
        "角色衍生",
        "人物服装、状态和时期变化的衍生约束",
    ),
    ManagedSkillFile("prop", "prop.md", "道具", "道具基础形象的生成约束"),
    ManagedSkillFile(
        "prop-derivative", "prop-derivative.md", "道具衍生", "道具状态和用途变化的衍生约束"
    ),
    ManagedSkillFile("scene", "scene.md", "场景", "场景基础形象与空间关系的生成约束"),
    ManagedSkillFile(
        "scene-derivative", "scene-derivative.md", "场景衍生", "场景时间、天气和状态变化的约束"
    ),
    ManagedSkillFile("storyboard", "storyboard.md", "分镜", "导演分镜提示词的视觉技法"),
    ManagedSkillFile(
        "storyboard-video",
        "storyboard-video.md",
        "分镜视频",
        "视频提示词中的视觉风格和连续性约束",
    ),
    ManagedSkillFile(
        "director-technique-rules",
        "technique-director-rules.md",
        "技法-导演规则",
        "色调、光影、质感、空间、音乐和环境音的全局约束",
    ),
    ManagedSkillFile(
        "storyboard-table-design",
        "technique-storyboard-table-design.md",
        "技法-分镜表设计",
        "分镜表的氛围、动作、环境动态、运镜和转场规范",
    ),
)

DIRECTOR_HANDBOOK_FILES = (
    ManagedSkillFile("readme", "README.md", "README", "叙事规划阶段使用的导演技法参考"),
    ManagedSkillFile(
        "director-planning", "director-planning.md", "导演规划", "主题、人物、冲突和节奏的规划方法"
    ),
    ManagedSkillFile(
        "storyboard-table", "storyboard-table.md", "分镜表", "镜头拆解、调度、声音和连续性规则"
    ),
)

SYSTEM_PROMPTS = (
    ("event-extraction", "事件提取", "从原文中提取按时间顺序排列、具备因果关系的事件。"),
    (
        "chapter-analysis",
        "章节分析",
        "基于原文章节提炼事件、人物关系、核心冲突、开场钩子、改编策略和风险。",
    ),
    (
        "script-generation",
        "短剧剧本生成",
        "结合原文、章节分析与参考版本生成包含场次、动作和台词的待审核短剧剧本。",
    ),
    ("script-review", "剧本审核", "独立审核短剧剧本的结构、逻辑、节奏、可视化程度和可制作性。"),
    ("script-repair", "剧本修复", "根据审核问题和用户意见局部修复或完整重写剧本。"),
    ("script-asset-extraction", "剧本资产提取", "提取角色、场景、道具及其衍生状态。"),
    (
        "asset-prompt-generation",
        "资产提示词生成",
        "根据资产说明和项目视觉手册生成可直接用于生图的提示词。",
    ),
    (
        "storyboard-generation",
        "分镜表生成",
        "依据生效剧本、塑造资产、视觉手册与导演手册生成结构化分镜表。",
    ),
    ("storyboard-review", "分镜审核", "审核分镜的叙事覆盖、轴线、连续性、节奏和执行可行性。"),
    ("storyboard-repair", "分镜修复", "根据审核问题和用户意见局部修复或重新生成分镜。"),
    ("video-prompt-generation", "视频提示词生成", "基于分镜和视觉手册生成可执行的视频模型提示词。"),
    ("voice-binding", "音色绑定", "依据角色年龄、性格、处境和台词情绪匹配音色。"),
    (
        "dialogue-extraction",
        "台词提取",
        "从生效剧本与分镜中提取可配音台词，并补充情绪和表演指导。",
    ),
)
SYSTEM_PROMPT_CODES = frozenset(code for code, _name, _description in SYSTEM_PROMPTS)
SYSTEM_PROMPT_BASELINE_VERSIONS = {
    "storyboard-generation": 4,
    "storyboard-review": 4,
    "storyboard-repair": 4,
    "video-prompt-generation": 8,
}
SYSTEM_PROMPT_TEMPLATE_ROOT = Path(__file__).with_name("system_prompt_templates")
LEGACY_HANDBOOK_MARKERS = {
    "ToonFlow": "包含其它产品名称 ToonFlow",
    "driector_skills": "包含拼写错误的旧目录 driector_skills",
}
MARKDOWN_FILE_REFERENCE = re.compile(r"`([^`\r\n]+\.md)`", re.IGNORECASE)

HANDBOOK_TASK_FILES: dict[str, dict[HandbookType, tuple[str, ...]]] = {
    "project-ai-creation": {
        HandbookType.VISUAL: ("README.md", "prefix.md", "technique-director-rules.md"),
        HandbookType.DIRECTOR: ("README.md", "director-planning.md"),
    },
    "chapter-analysis": {
        HandbookType.VISUAL: ("README.md", "prefix.md"),
        HandbookType.DIRECTOR: ("README.md", "director-planning.md"),
    },
    "script-generation": {
        HandbookType.VISUAL: ("README.md", "prefix.md"),
        HandbookType.DIRECTOR: ("README.md", "director-planning.md"),
    },
    "script-review": {
        HandbookType.VISUAL: ("README.md",),
        HandbookType.DIRECTOR: ("README.md", "director-planning.md"),
    },
    "script-repair": {
        HandbookType.VISUAL: ("README.md", "prefix.md"),
        HandbookType.DIRECTOR: ("README.md", "director-planning.md"),
    },
    "script-asset-extraction": {
        HandbookType.VISUAL: ("README.md", "character.md", "scene.md", "prop.md"),
        HandbookType.DIRECTOR: ("README.md",),
    },
    "asset-prompt-generation": {
        HandbookType.VISUAL: (
            "README.md",
            "prefix.md",
            "character.md",
            "character-derivative.md",
            "scene.md",
            "scene-derivative.md",
            "prop.md",
            "prop-derivative.md",
        ),
        HandbookType.DIRECTOR: ("README.md",),
    },
    "storyboard-generation": {
        HandbookType.VISUAL: (
            "README.md",
            "prefix.md",
            "storyboard.md",
            "technique-director-rules.md",
            "technique-storyboard-table-design.md",
        ),
        HandbookType.DIRECTOR: (
            "README.md",
            "director-planning.md",
            "storyboard-table.md",
        ),
    },
    "storyboard-review": {
        HandbookType.VISUAL: (
            "README.md",
            "storyboard.md",
            "technique-storyboard-table-design.md",
        ),
        HandbookType.DIRECTOR: ("README.md", "storyboard-table.md"),
    },
    "storyboard-repair": {
        HandbookType.VISUAL: (
            "README.md",
            "prefix.md",
            "storyboard.md",
            "technique-director-rules.md",
            "technique-storyboard-table-design.md",
        ),
        HandbookType.DIRECTOR: (
            "README.md",
            "director-planning.md",
            "storyboard-table.md",
        ),
    },
    "video-prompt-generation": {
        HandbookType.VISUAL: ("README.md", "prefix.md", "storyboard-video.md"),
        HandbookType.DIRECTOR: ("README.md", "storyboard-table.md"),
    },
    "dialogue-extraction": {
        HandbookType.VISUAL: ("README.md",),
        HandbookType.DIRECTOR: ("README.md", "director-planning.md"),
    },
    "voice-binding": {
        HandbookType.VISUAL: ("README.md", "character.md"),
        HandbookType.DIRECTOR: ("README.md", "director-planning.md"),
    },
}


def default_system_prompt_content(code: str) -> str:
    if code not in SYSTEM_PROMPT_CODES:
        raise ValueError(f"未知系统提示词：{code}")
    path = SYSTEM_PROMPT_TEMPLATE_ROOT / f"{code}.md"
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"系统提示词模板不能为空：{code}")
    return content


def internal_system_prompt_content(filename: str) -> str:
    """Load a non-admin protocol appendix kept outside the managed prompt catalog."""
    if Path(filename).name != filename or not filename.endswith(".md"):
        raise ValueError("内部系统提示词文件名无效")
    path = SYSTEM_PROMPT_TEMPLATE_ROOT / filename
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"内部系统提示词模板不能为空：{filename}")
    return content


def handbook_manifest(handbook_type: HandbookType) -> tuple[ManagedSkillFile, ...]:
    return VISUAL_HANDBOOK_FILES if handbook_type == HandbookType.VISUAL else DIRECTOR_HANDBOOK_FILES


def handbook_folder(handbook_type: HandbookType) -> str:
    return "visual-handbooks" if handbook_type == HandbookType.VISUAL else "director-handbooks"


def tenant_skills_root(tenant_id: str) -> Path:
    root = (get_settings().skills_root / "tenants" / tenant_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def public_to_stored_path(tenant_id: str, public_path: str) -> str:
    return f"tenants/{tenant_id}/{public_path}"


def stored_to_public_path(tenant_id: str, stored_path: str) -> str:
    prefix = f"tenants/{tenant_id}/"
    return stored_path.removeprefix(prefix)


def resolve_tenant_skill_path(
    tenant_id: str,
    public_path: str,
    *,
    must_exist: bool = True,
) -> Path:
    if not public_path or Path(public_path).is_absolute():
        raise ValueError("Skills 路径无效")
    root = tenant_skills_root(tenant_id)
    candidate = (root / public_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("禁止访问当前租户 Skills 根目录以外的文件")
    if candidate.suffix.lower() not in ALLOWED_SKILL_EXTENSIONS:
        raise ValueError("不支持编辑此文件类型")
    if must_exist and (not candidate.exists() or not candidate.is_file()):
        raise FileNotFoundError(public_path)
    return candidate


def _safe_package_name(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return candidate[:80] or fallback


def _default_handbook_content(handbook: Handbook, item: ManagedSkillFile) -> str:
    if item.key == "readme":
        description = handbook.description.strip() or "请在此定义手册的核心方法、适用范围与禁止事项。"
        return (
            f"# {handbook.name}\n\n{description}\n\n"
            "## 使用原则\n\n"
            "- 所有生成结果必须服从本手册及其子文件约束。\n"
            "- 发生冲突时，以更具体的资产或分镜规则为准。\n"
            "- 不得忽略项目设定、生效剧本和已确认资产。\n"
        )
    return (
        f"# {item.label}\n\n"
        f"## 目标\n\n{item.purpose}。\n\n"
        "## 约束\n\n"
        "- 保持项目设定、人物身份与叙事上下文一致。\n"
        "- 输出必须具体、可执行，并避免互相冲突的描述。\n"
        "- 未经项目上下文确认，不得擅自增加关键设定。\n"
    )


def validate_handbook_files(
    handbook_type: HandbookType,
    files: dict[str, str],
) -> dict[str, str]:
    required = {item.filename for item in handbook_manifest(handbook_type)}
    supplied = set(files)
    if supplied != required:
        missing = sorted(required - supplied)
        extra = sorted(supplied - required)
        details: list[str] = []
        if missing:
            details.append(f"缺少固定文件：{'、'.join(missing)}")
        if extra:
            details.append(f"包含非固定文件：{'、'.join(extra)}")
        raise ValueError("；".join(details))
    normalized = {name: content.strip() for name, content in files.items()}
    empty = sorted(name for name, content in normalized.items() if not content)
    if empty:
        raise ValueError(f"固定文件不能为空：{'、'.join(empty)}")
    oversized = sorted(name for name, content in normalized.items() if len(content) > 1_000_000)
    if oversized:
        raise ValueError(f"固定文件内容过长：{'、'.join(oversized)}")
    allowed_references = {item.filename for item in handbook_manifest(handbook_type)}
    issues: list[str] = []
    for filename, content in normalized.items():
        for marker, message in LEGACY_HANDBOOK_MARKERS.items():
            if marker.casefold() in content.casefold():
                issues.append(f"{filename} {message}")
        for reference in MARKDOWN_FILE_REFERENCE.findall(content):
            referenced_name = Path(reference.replace("\\", "/")).name
            if referenced_name not in allowed_references:
                issues.append(f"{filename} 引用了不存在的固定文件 {reference}")
    if issues:
        raise ValueError("手册内容校验失败：" + "；".join(sorted(set(issues))))
    return normalized


def ensure_handbook_package(handbook: Handbook) -> None:
    root = get_settings().skills_root.resolve()
    tenant_root = tenant_skills_root(handbook.tenant_id)
    public_path = stored_to_public_path(handbook.tenant_id, handbook.skill_path)
    expected_folder = handbook_folder(handbook.handbook_type)
    canonical_public_path = f"{expected_folder}/{handbook.id}"

    if public_path != canonical_public_path:
        legacy_path = (root / handbook.skill_path).resolve()
        public_path = canonical_public_path
        package_root = (tenant_root / public_path).resolve()
        package_root.mkdir(parents=True, exist_ok=True)
        if legacy_path.is_relative_to(root) and legacy_path.is_dir():
            allowed_names = {item.filename for item in handbook_manifest(handbook.handbook_type)}
            for source in legacy_path.iterdir():
                if source.is_file() and source.name in allowed_names:
                    target = package_root / source.name
                    if not target.exists():
                        target.write_bytes(source.read_bytes())
        handbook.skill_path = public_to_stored_path(handbook.tenant_id, public_path)

    package_root = (tenant_root / public_path).resolve()
    if not package_root.is_relative_to(tenant_root):
        raise ValueError("手册 Skills 路径无效")
    package_root.mkdir(parents=True, exist_ok=True)
    for item in handbook_manifest(handbook.handbook_type):
        path = package_root / item.filename
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            path.write_text(_default_handbook_content(handbook, item), encoding="utf-8", newline="\n")


def create_handbook_package(handbook: Handbook, files: dict[str, str] | None = None) -> None:
    public_path = f"{handbook_folder(handbook.handbook_type)}/{handbook.id}"
    handbook.skill_path = public_to_stored_path(handbook.tenant_id, public_path)
    package_root = tenant_skills_root(handbook.tenant_id) / public_path
    package_root.mkdir(parents=True, exist_ok=True)
    if files is None:
        files = {
            item.filename: _default_handbook_content(handbook, item)
            for item in handbook_manifest(handbook.handbook_type)
        }
    replace_handbook_files(handbook, files)


def read_handbook_files(handbook: Handbook) -> list[dict[str, str]]:
    ensure_handbook_package(handbook)
    package_root = get_settings().skills_root.resolve() / handbook.skill_path
    return [
        {
            "key": item.key,
            "filename": item.filename,
            "label": item.label,
            "purpose": item.purpose,
            "content": (package_root / item.filename).read_text(encoding="utf-8"),
        }
        for item in handbook_manifest(handbook.handbook_type)
    ]


def replace_handbook_files(handbook: Handbook, files: dict[str, str]) -> None:
    normalized = validate_handbook_files(handbook.handbook_type, files)
    package_root = get_settings().skills_root.resolve() / handbook.skill_path
    package_root.mkdir(parents=True, exist_ok=True)
    for filename, content in normalized.items():
        temporary = package_root / f".{filename}.tmp"
        temporary.write_text(f"{content}\n", encoding="utf-8", newline="\n")
        temporary.replace(package_root / filename)


def handbook_usage_instructions(
    handbooks: list[Handbook],
    *,
    prompt_code: str | None = None,
) -> str:
    """Build explicit runtime instructions for the project-selected handbook snapshots."""
    task_files = HANDBOOK_TASK_FILES.get(prompt_code or "", {})
    lines = [
        "项目创作手册（强制执行）：",
        "- 下列技能包是当前项目明确选择并由平台冻结的版本，不得替换为其它手册或模型常识。",
        "- 开始创作前必须调用对应 Skill，并用 Read 读取列出的资源；不得仅凭技能名称推测内容。",
    ]
    for handbook in handbooks:
        handbook_label = "视觉手册" if handbook.handbook_type == HandbookType.VISUAL else "导演手册"
        required_files = task_files.get(handbook.handbook_type)
        if required_files is None:
            required_files = (
                ("README.md", "prefix.md")
                if handbook.handbook_type == HandbookType.VISUAL
                else ("README.md", "director-planning.md", "storyboard-table.md")
            )
        public_path = stored_to_public_path(handbook.tenant_id, handbook.skill_path)
        lines.append(
            f"- {handbook_label}《{handbook.name}》v{handbook.version}：技能根 {public_path}；"
            f"本次至少读取 {', '.join(required_files)}。"
        )
    if prompt_code is None:
        lines.append("- 再根据用户当前意图读取该技能包内更具体的资产、分镜或视频规则文件。")
    lines.append("- 如项目手册与用户已确认的项目事实冲突，以项目事实为准，并向用户说明冲突。")
    return "\n".join(lines)


def prompt_public_path(code: str) -> str:
    return f"system-prompts/{code}.md"


def write_prompt_file(prompt: PromptTemplate) -> None:
    path = resolve_tenant_skill_path(
        prompt.tenant_id,
        prompt_public_path(prompt.code),
        must_exist=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{prompt.content.strip()}\n", encoding="utf-8", newline="\n")


def ensure_prompt_file(prompt: PromptTemplate) -> None:
    path = resolve_tenant_skill_path(
        prompt.tenant_id,
        prompt_public_path(prompt.code),
        must_exist=False,
    )
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        write_prompt_file(prompt)
