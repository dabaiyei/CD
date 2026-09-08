from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chapter, ChapterStatus, User, UserSkill, UserSkillStage

USER_SKILL_COMMAND_FILE = "cineforge-user-skill.json"

_USER_SKILL_CHANGE_REQUEST = re.compile(
    r"(?:(?:保存|存为|创建|新增|添加|建立|写入|更新|修改|编辑|改写|启用|禁用|删除)"
    r".{0,16}(?:skill|技能)|(?:skill|技能).{0,16}"
    r"(?:保存|存为|创建|新增|添加|建立|写入|更新|修改|编辑|改写|启用|禁用|删除))",
    re.I,
)
_USER_SKILL_CHANGE_CONFIRMATION = re.compile(
    r"^(?:确认|确定|可以|好的?|是的|就这样|保存吧|提交吧|创建吧|新增吧|更新吧|修改吧)[。！!\s]*$"
)

USER_SKILL_STAGE_LABELS: dict[UserSkillStage, str] = {
    UserSkillStage.SCRIPT_GENERATION: "剧本生成",
    UserSkillStage.SCRIPT_REVIEW: "剧本审核与修复",
    UserSkillStage.ASSET_EXTRACTION: "资产提取",
    UserSkillStage.ASSET_PROMPT_GENERATION: "资产提示词生成",
    UserSkillStage.STORYBOARD_GENERATION: "分镜生成",
    UserSkillStage.STORYBOARD_REVIEW: "分镜审核与修复",
    UserSkillStage.VIDEO_GENERATION: "视频提示词与视频生成",
}

PROMPT_USER_SKILL_STAGES: dict[str, tuple[UserSkillStage, ...]] = {
    "project-ai-creation": (UserSkillStage.SCRIPT_GENERATION,),
    "script-generation": (UserSkillStage.SCRIPT_GENERATION,),
    "script-review": (UserSkillStage.SCRIPT_REVIEW,),
    "script-repair": (UserSkillStage.SCRIPT_GENERATION, UserSkillStage.SCRIPT_REVIEW),
    "script-asset-extraction": (UserSkillStage.ASSET_EXTRACTION,),
    "asset-prompt-generation": (UserSkillStage.ASSET_PROMPT_GENERATION,),
    "storyboard-generation": (UserSkillStage.STORYBOARD_GENERATION,),
    "storyboard-review": (UserSkillStage.STORYBOARD_REVIEW,),
    "storyboard-repair": (
        UserSkillStage.STORYBOARD_GENERATION,
        UserSkillStage.STORYBOARD_REVIEW,
    ),
    "video-prompt-generation": (UserSkillStage.VIDEO_GENERATION,),
}


def prompt_user_skill_stages(prompt_code: str) -> tuple[UserSkillStage, ...]:
    return PROMPT_USER_SKILL_STAGES.get(prompt_code, ())


def infer_chat_user_skill_stages(
    content: str,
    *,
    chapter: Chapter | None = None,
) -> tuple[UserSkillStage, ...]:
    normalized = content.casefold()
    stages: list[UserSkillStage] = []

    def include(stage: UserSkillStage) -> None:
        if stage not in stages:
            stages.append(stage)

    if any(keyword in normalized for keyword in ("视频提示词", "生成视频", "视频生成", "成片")):
        include(UserSkillStage.VIDEO_GENERATION)
    if any(keyword in normalized for keyword in ("分镜", "镜头", "运镜")):
        include(
            UserSkillStage.STORYBOARD_REVIEW
            if any(keyword in normalized for keyword in ("审核", "检查", "修复", "修改", "重做"))
            else UserSkillStage.STORYBOARD_GENERATION
        )
    if any(keyword in normalized for keyword in ("资产提示词", "生图提示词", "形象提示词")):
        include(UserSkillStage.ASSET_PROMPT_GENERATION)
    elif any(keyword in normalized for keyword in ("资产", "人物提取", "场景提取", "道具提取")):
        include(UserSkillStage.ASSET_EXTRACTION)
    if any(keyword in normalized for keyword in ("剧本", "改编", "台词")):
        include(
            UserSkillStage.SCRIPT_REVIEW
            if any(keyword in normalized for keyword in ("审核", "检查", "修复", "修改", "重写"))
            else UserSkillStage.SCRIPT_GENERATION
        )

    if not stages and chapter is not None:
        status_stage = {
            ChapterStatus.UNINITIALIZED: UserSkillStage.SCRIPT_GENERATION,
            ChapterStatus.ANALYZING: UserSkillStage.SCRIPT_GENERATION,
            ChapterStatus.ANALYZED: UserSkillStage.SCRIPT_GENERATION,
            ChapterStatus.SCRIPTING: UserSkillStage.SCRIPT_GENERATION,
            ChapterStatus.REVIEWING: UserSkillStage.SCRIPT_REVIEW,
            ChapterStatus.ASSETS: UserSkillStage.ASSET_EXTRACTION,
            ChapterStatus.STORYBOARD: UserSkillStage.STORYBOARD_GENERATION,
            ChapterStatus.VIDEO: UserSkillStage.VIDEO_GENERATION,
            ChapterStatus.COMPLETED: UserSkillStage.VIDEO_GENERATION,
        }.get(chapter.status)
        if status_stage is not None:
            include(status_stage)
    return tuple(stages)


async def enabled_user_skills(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    stages: tuple[UserSkillStage, ...] | list[UserSkillStage] | None,
) -> list[UserSkill]:
    rows = list(
        (
            await session.scalars(
                select(UserSkill)
                .where(
                    UserSkill.tenant_id == tenant_id,
                    UserSkill.user_id == user_id,
                    UserSkill.enabled.is_(True),
                )
                .order_by(UserSkill.name, UserSkill.id)
            )
        ).all()
    )
    if stages is None:
        return rows
    expected = {stage.value for stage in stages}
    if not expected:
        return []
    return [row for row in rows if expected.intersection(row.trigger_stages or [])]


def _catalog_excerpt(description: str, *, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", description).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1].rstrip()}…"


def user_skill_snapshots(skills: list[UserSkill]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    snapshots: list[dict[str, Any]] = []
    versions: dict[str, str] = {}
    for skill in skills:
        root = f"user-skills/{skill.id}"
        stage_labels = [
            USER_SKILL_STAGE_LABELS[UserSkillStage(stage)]
            for stage in skill.trigger_stages
            if stage in UserSkillStage._value2member_map_
        ]
        content = (
            f"# {skill.name}\n\n"
            f"> 检索说明：{_catalog_excerpt(skill.description)}\n\n"
            f"> 适用阶段：{'、'.join(stage_labels)}\n\n"
            "## 技能规则\n\n"
            f"{skill.description.strip()}\n"
        )
        snapshots.append(
            {
                "path": f"{root}/README.md",
                "content": content,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "version": str(skill.version),
            }
        )
        versions[root] = str(skill.version)
    return snapshots, versions


def user_skill_usage_instructions(
    skills: list[UserSkill],
    *,
    stages: tuple[UserSkillStage, ...] | list[UserSkillStage] | None,
    selected_skills: list[UserSkill] | None = None,
) -> str:
    if not skills:
        return ""
    stage_text = (
        "全部个人技能"
        if stages is None
        else "、".join(USER_SKILL_STAGE_LABELS[stage] for stage in stages)
    )
    selected = selected_skills or []
    selected_instruction = ""
    if selected:
        selected_paths = "、".join(
            f"{item.name}（user-skills/{item.id}/README.md）" for item in selected
        )
        selected_instruction = (
            f"\n- 用户通过斜杠命令显式绑定了：{selected_paths}。"
            "这些 Skill 是本轮硬约束，必须逐个调用 Skill 工具读取 README.md 后再回答；"
            "不能只根据名称或目录说明推测正文。"
        )
    return (
        f"\n\n用户个人 Skills（当前检索范围：{stage_text}）：\n"
        "- 平台只向你暴露匹配阶段的 Skill 名称与检索说明；不要假设未调用 Skill 的正文内容。\n"
        "- 根据当前任务选择真正相关的 Skill，可同时调用多个；调用后必须读取其 README.md 并执行。\n"
        "- 个人 Skill 是用户补充规则；与系统安全、平台数据事实冲突时，以系统规则和平台事实为准。"
        f"{selected_instruction}"
    )


def personal_agent_scope_id(user_id: str) -> str:
    return f"personal-{user_id}"


def personal_skill_change_requested(content: str, *, mode: str) -> bool:
    text = content.strip()
    if not text:
        return False
    if _USER_SKILL_CHANGE_REQUEST.search(text):
        return True
    return mode == "skill" and bool(_USER_SKILL_CHANGE_CONFIRMATION.fullmatch(text))


def personal_agent_system_instructions(
    skills: list[UserSkill],
    *,
    mode: str = "chat",
    allow_skill_changes: bool = False,
) -> str:
    catalog = "\n".join(
        f"- {item.name}（ID: {item.id}；阶段: "
        f"{'、'.join(USER_SKILL_STAGE_LABELS[UserSkillStage(stage)] for stage in item.trigger_stages)}；"
        f"状态: {'启用' if item.enabled else '禁用'}）"
        for item in skills
    ) or "- 暂无个人 Skill"
    mode_instruction = {
        "image": (
            "本轮是连续对话式的生成图片模式，不是一次性提示词表单。先与用户保持自然交流，"
            "读取最近对话、上一轮媒体提示词与可用历史图片，再综合用户文字、参考图片和已调用 Skill。"
            "用户只提出局部修改时，必须保留上一轮未要求改变的主体、构图、风格和细节，"
            "将本轮修改合并成一份完整提示词，不能只输出差异描述。"
            "整理出可直接交给图片模型的完整提示词。最终只能返回严格 JSON，不要使用 Markdown 代码块："
            '{"message":"面向用户的简短中文回复","prompt":"最终图片提示词"}。'
            "message 不得声称图片已经生成；平台会在你返回后调用真实图片模型。"
        ),
        "video": (
            "本轮是连续对话式的生成视频模式，不是一次性提示词表单。先与用户保持自然交流，"
            "读取最近对话、上一轮媒体提示词与可用历史参考图，再综合用户文字、参考图片和已调用 Skill。"
            "用户只提出局部修改时，必须沿用上一轮未要求改变的画面内容、动作、运镜和风格，"
            "将修改合并成一份可独立执行的完整视频提示词，不能只输出差异描述。"
            "整理出可直接交给视频模型的完整提示词。最终只能返回严格 JSON，不要使用 Markdown 代码块："
            '{"message":"面向用户的简短中文回复","prompt":"最终视频提示词"}。'
            "若包含中文台词，台词原文必须保持中文且逐字保留。message 不得声称视频已经生成；"
            "平台会在你返回后调用真实视频模型。"
        ),
        "skill": (
            "本轮是创作 Skill 模式。重点帮助用户分析、设计、保存或修改个人 Skill，"
            "仍可正常交流；只有用户确认保存或修改时才写入受控 Skill 命令。"
        ),
    }.get(mode, "本轮是普通对话模式。保持自然交流，并按需检索和调用相关个人 Skill。")
    chat_contract = (
        "普通对话先正常输出给用户看的中文回复。只有用户确实要求生成图片或视频时，"
        "在回复最后另起一行追加一个平台动作标记，标记后不要再输出其它文字："
        '<CINEFORGE_MEDIA>{"type":"image"或"video",'
        '"generation_mode":"text_to_image"或"image_to_image"或"text_to_video"或"image_to_video",'
        '"prompt":"交给媒体模型的完整提示词","model_id":"用户明确指定的模型 ID 或模型名，可省略",'
        '"resolution":"可省略","aspect_ratio":"可省略","duration_seconds":5,'
        '"reference_attachment_ids":["需要引用的图片附件 ID"]}</CINEFORGE_MEDIA>。'
        "标记中的 JSON 必须严格合法且不要使用 Markdown 代码围栏。"
        "模型和参数不明确时省略对应字段，由平台使用管理员配置的默认模型和模型能力默认值。"
        "如果只是咨询、分析或讨论，不要输出该标记。"
        "有当前或历史对话图片且用户说‘基于这张图、参考上图、做类似风格’时，"
        "优先使用 image_to_image 或 image_to_video，"
        "并在 reference_attachment_ids 中填写可用附件 ID；没有参考图时使用 text_to_image 或 text_to_video。"
        "不得声称媒体已经生成，平台会在解析 media 动作后调用真实模型并返回任务状态。"
    )
    skill_change_instruction = (
        f"本轮用户已明确授权保存或修改个人 Skill。只能使用 Write 创建 "
        f"project-files/new/{USER_SKILL_COMMAND_FILE}，内容必须是严格 JSON："
        '{"operation":"upsert_user_skill","skill_id":"修改时填写，新增时为空",'
        '"name":"技能名称","trigger_stages":["storyboard_generation"],'
        '"description":"完整技能说明与规则","enabled":true}。'
        "trigger_stages 只能使用 script_generation、script_review、asset_extraction、"
        "asset_prompt_generation、storyboard_generation、storyboard_review、video_generation。"
        "修改前应根据目录 ID 调用对应 Skill 阅读现有正文；新增时 skill_id 留空。"
        "除该受控命令外，不要创建其它文件。完成后只需简洁说明保存或修改了什么。"
        if allow_skill_changes
        else (
            "本轮用户没有明确授权保存、创建或修改个人 Skill，严禁调用 Write 或 Edit，"
            "也不得创建任何文件或声称 Skill 已保存。‘使用/调用某个 Skill 或美学风格’"
            "仅代表应用规则，不代表保存或修改 Skill。"
        )
    )
    return (
        "你是用户的独立个人创作 Agent，不绑定任何短剧项目。你不能读取、修改、选择或操作项目、章节、"
        "资产、分镜和视频任务，也不能声称已经替用户完成项目操作。你可以讨论创作方法、分析用户粘贴的"
        "Skill 文本，并受控地新增或修改当前用户自己的 Skill。\n"
        f"当前工作模式：{mode_instruction}\n"
        "个人 Skill 目录：\n"
        f"{catalog}\n"
        f"{chat_contract if mode == 'chat' else ''}\n"
        "禁用状态的 Skill 只能在用户要求查看或修改它时调用，不能作为当前创作建议的生效规则。"
        f"{skill_change_instruction}"
    )


class UserSkillUpsertCommand(BaseModel):
    operation: str
    skill_id: str | None = Field(default=None, max_length=36)
    name: str = Field(min_length=1, max_length=120)
    trigger_stages: list[UserSkillStage] = Field(min_length=1, max_length=7)
    description: str = Field(min_length=1, max_length=200_000)
    enabled: bool = True

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        if value != "upsert_user_skill":
            raise ValueError("unsupported user skill operation")
        return value

    @field_validator("name", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("field cannot be blank")
        return normalized

    @field_validator("trigger_stages")
    @classmethod
    def unique_stages(cls, value: list[UserSkillStage]) -> list[UserSkillStage]:
        return list(dict.fromkeys(value))


async def apply_personal_agent_skill_changes(
    session: AsyncSession,
    *,
    user: User,
    changes: list[Any],
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for change in changes:
        operation = str(getattr(change, "operation", ""))
        name = str(getattr(change, "name", "") or "")
        content = getattr(change, "content", None)
        if operation != "create" or name != USER_SKILL_COMMAND_FILE or not isinstance(content, str):
            outcomes.append(
                {
                    "operation": "batch",
                    "file_id": None,
                    "name": name or "未授权文件操作",
                    "status": "rejected",
                    "reason": "个人 Agent 只能通过受控命令新增或修改个人 Skill",
                }
            )
            continue
        try:
            command = UserSkillUpsertCommand.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as error:
            outcomes.append(
                {
                    "operation": "upsert_user_skill",
                    "file_id": None,
                    "name": "个人 Skill",
                    "status": "rejected",
                    "reason": f"Skill 命令格式无效：{error}",
                }
            )
            continue

        skill = await session.get(UserSkill, command.skill_id) if command.skill_id else None
        if skill is not None and (skill.tenant_id != user.tenant_id or skill.user_id != user.id):
            skill = None
        if command.skill_id and skill is None:
            outcomes.append(
                {
                    "operation": "upsert_user_skill",
                    "file_id": None,
                    "name": command.name,
                    "status": "rejected",
                    "reason": "要修改的个人 Skill 不存在",
                }
            )
            continue
        duplicate = await session.scalar(
            select(UserSkill).where(
                UserSkill.tenant_id == user.tenant_id,
                UserSkill.user_id == user.id,
                func.lower(UserSkill.name) == command.name.casefold(),
                *([UserSkill.id != skill.id] if skill is not None else []),
            )
        )
        if duplicate is not None:
            outcomes.append(
                {
                    "operation": "upsert_user_skill",
                    "file_id": duplicate.id,
                    "name": command.name,
                    "status": "conflict",
                    "reason": "已有同名个人 Skill",
                }
            )
            continue
        created = skill is None
        if skill is None:
            skill = UserSkill(tenant_id=user.tenant_id, user_id=user.id, name=command.name)
            session.add(skill)
        else:
            skill.version += 1
        skill.name = command.name
        skill.description = command.description
        skill.trigger_stages = [stage.value for stage in command.trigger_stages]
        skill.enabled = command.enabled
        await session.flush()
        outcomes.append(
            {
                "operation": "upsert_user_skill",
                "file_id": skill.id,
                "name": skill.name,
                "status": "applied",
                "reason": None,
                "resource_type": "user_skill",
                "resource_id": skill.id,
                "change_type": "created" if created else "updated",
                "version": skill.version,
            }
        )
    return outcomes
