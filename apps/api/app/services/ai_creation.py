"""Original-project creation contracts, durable artifacts and chapter prerequisites."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIModel, Chapter, Project, ProjectFile, ProjectFileKind, ScriptVersion


class CreationPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class CreationPreferences(CreationPayload):
    genre: str = Field(min_length=1, max_length=300)
    chapter_count: int = Field(ge=1, le=100)
    chapter_duration_seconds: int = Field(ge=15, le=7200)
    feedback: str = Field(default="", max_length=6000)


class StoryProposal(CreationPayload):
    title: str = Field(min_length=1, max_length=120)
    introduction: str = Field(min_length=1, max_length=4000)
    premise: str = Field(min_length=1, max_length=8000)


class ProposalResult(BaseModel):
    proposals: list[StoryProposal] = Field(min_length=3, max_length=5)


class ChapterPremise(CreationPayload):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=12000)


class OutlineResult(BaseModel):
    outline: str = Field(min_length=1, max_length=100000)
    story_bible: str = Field(min_length=1, max_length=60000)
    chapters: list[ChapterPremise] = Field(min_length=1, max_length=100)


class OutlinePlan(BaseModel):
    outline: str = Field(min_length=1, max_length=100000)
    story_bible: str = Field(min_length=1, max_length=60000)
    chapter_titles: list[str] = Field(min_length=1, max_length=100)


class ChapterBatchResult(BaseModel):
    chapters: list[ChapterPremise] = Field(min_length=1, max_length=5)


class CreationAction(BaseModel):
    action: Literal["propose", "choose", "retry"]
    preferences: CreationPreferences | None = None
    proposal_index: int | None = Field(default=None, ge=0, le=4)
    revision: int = Field(ge=0)


CINEMATIC_GUIDANCE = """电影制作强制规范（落实到当前阶段输出，不能只写“电影质感”）：
1. 剧情：每场戏明确人物目标、阻力、选择与代价，冲突通过可见行动推进；
铺垫与回收有因果，避免旁白解释一切。章节具备起承转合与有效悬念，不用无意义快切填时长。
2. 动作：描述准备、发力、接触、惯性与收势；遵循重心、重量、碰撞及受力方向。
复杂打斗拆为可连续实现的动作单元，记录左右手、持物、受伤和位置连续性。
3. 镜头与时长：由戏剧节拍决定长短镜头，在模型合法时长内给完整动作、情绪转折和环境建立足够时间。
每镜一个主要叙事目的，交代起始构图、主体运动、摄影机路径及结束画面；
静止镜头也有明确表达，不机械堆叠推拉摇移。
4. 机位：明确景别、视线高度、俯仰角、镜头透视和前中后景关系。建立空间轴线与视线匹配，
跨轴须有可见过渡，避免无动机跳轴、穿模、主体瞬移和无意义广角变形。
5. 光影：指定有动机的主光源、方向、软硬、色温、明暗对比和轮廓分离；补光保持面部可读。
同场光向和曝光连续，环境变化必须有来源，不用随机霓虹代替电影照明。
6. 材质与场景：服装、皮肤、金属、木石和空气各有可辨粗糙度、磨损、反射与尺度；
空间具有可行走路径、遮挡和可信生活痕迹。保持所选画风，不把二维风格强改成写实摄影。
7. 特效：服务叙事且有明确起点、传播方向、亮度衰减和环境交互；烟尘、碎屑、能量与实物遮挡一致，
保留角色轮廓和动作可读性，避免全屏泛光掩盖细节。
8. 台词与表演：通过动作、停顿、视线、潜台词表达情绪。保留角色身份和说话习惯，中文对白逐字保留；
为说话、呼吸和反应留时间，不擅加外语、旁白或口型动作。
9. 声音：明确每段对白的说话人、时间及是否画内；同一时段避免无意重叠。
区分环境底声、动作拟音和配乐，指定层次和进入退出；遵循当前视频模型音频能力及用户音频开关，
禁用音频时不虚构音轨，支持音频时不能一律写成无声。
10. 验收：剧本核验因果与可拍性，资产提示词核验造型与材质，分镜核验轴线、动作和时间预算，
视频提示词核验参考图/音频绑定与连续性。模型能力和固定画幅为硬约束，
不允许为电影化超出参考数量、格式、时长或比例。
"""


async def project_creation_guidance(
    session: AsyncSession, project: Project, *, include_story: bool = True
) -> str:
    sections: list[str] = []
    if project.creation_mode == "ai" and include_story:
        preferences = (project.creation_state or {}).get("preferences", {})
        sections.append(
            f"本项目是原创 AI 剧本项目，固定画幅 {project.aspect_ratio}，不可更改。"
            "章节原始内容是创作提纲，不是完整小说；需遵循大纲与设定原创扩写场景、动作和对白，"
            "不能把几句基础描写当成完成的剧本，也不能虚构与设定矛盾的关键事实。"
            "剧本调度、人物站位、场景空间、分镜构图、资产图及视频均须适配该画幅。"
            f"每章目标成片时长 {preferences.get('chapter_duration_seconds', '待确认')} 秒；"
            "这是整章总时长，不能误作单镜时长，单镜须服从项目视频模型能力。"
        )
        for kind in ("text", "image", "video"):
            model_id = getattr(project, f"{kind}_model_id")
            model = await session.get(AIModel, model_id) if model_id else None
            if model:
                capabilities = {
                    key: value
                    for key, value in (model.capabilities or {}).items()
                    if key
                    in {
                        "generation_modes",
                        "duration_resolution_map",
                        "durations",
                        "resolutions",
                        "aspect_ratios",
                        "reference_limits",
                        "audio_policy",
                        "input_mime_types",
                        "output_formats",
                        "prompt_languages",
                        "negative_prompt_supported",
                    }
                }
                sections.append(
                    f"项目 {kind} 模型：{model.name}（{model.model_id}）；能力："
                    + json.dumps(capabilities, ensure_ascii=False)
                )
        bible = await session.scalar(
            select(ProjectFile).where(
                ProjectFile.project_id == project.id,
                or_(
                    ProjectFile.name == "创作记忆与设定.md",
                    ProjectFile.file_metadata["role"].as_string() == "ai_creation_bible",
                ),
                ProjectFile.kind == ProjectFileKind.MEMORY,
            )
        )
        if bible and bible.content:
            sections.append(f"项目记忆与设定（节选；需要细节时按需读取项目文件）：\n{bible.content[:4000]}")
        outline = await session.scalar(
            select(ProjectFile).where(
                ProjectFile.project_id == project.id,
                ProjectFile.name == "故事大纲.md",
            )
        )
        if outline and outline.content:
            sections.append(f"故事大纲（节选，不代表已生成章节正文）：\n{outline.content[:3000]}")
        sections.append(
            "只创作用户当前选择的章节，不自动扩写后续章节。"
            "记忆仅作为内部参考，不属于剧本正文，不得把记忆状态、总结或执行说明写进场次和动作。"
            "若输出结构提供 continuity_summary，将连续性记忆写入该独立字段；"
            "否则不要额外输出记忆，由平台单独维护。禁止加载全部历史剧本。"
        )
    if project.cinematic:
        sections.append(CINEMATIC_GUIDANCE)
    return "\n\n".join(sections)


async def require_ai_chapter_unlocked(session: AsyncSession, chapter: Chapter) -> None:
    project = await session.get(Project, chapter.project_id)
    if project is None or project.creation_mode != "ai":
        return
    has_script = (
        select(ScriptVersion.id)
        .where(
            ScriptVersion.chapter_id == Chapter.id,
            func.length(func.trim(ScriptVersion.content)) > 0,
        )
        .exists()
    )
    incomplete = await session.scalar(
        select(Chapter.id)
        .where(
            Chapter.project_id == project.id,
            Chapter.order_index < chapter.order_index,
            ~has_script,
        )
        .limit(1)
    )
    if incomplete:
        raise HTTPException(status_code=409, detail="请先完成前面章节的剧本创作，再进入本章")


async def chapter_continuity_context(session: AsyncSession, chapter: Chapter) -> str:
    """Only the two preceding effective scripts; never use future or stale versions."""
    previous = (
        await session.scalars(
            select(Chapter)
            .where(
                Chapter.project_id == chapter.project_id,
                Chapter.order_index < chapter.order_index,
            )
            .order_by(Chapter.order_index.desc())
            .limit(2)
        )
    ).all()
    parts = []
    for row in reversed(previous):
        script = (
            await session.get(ScriptVersion, row.active_script_version_id)
            if row.active_script_version_id
            else None
        )
        if not script:
            script = await session.scalar(
                select(ScriptVersion)
                .where(
                    ScriptVersion.chapter_id == row.id,
                )
                .order_by(ScriptVersion.version.desc())
                .limit(1)
            )
        if not script:
            continue
        memory = await session.scalar(
            select(ProjectFile)
            .where(
                ProjectFile.project_id == chapter.project_id,
                ProjectFile.kind == ProjectFileKind.MEMORY,
                ProjectFile.file_metadata["script_version_id"].as_string() == script.id,
            )
            .limit(1)
        )
        content = (
            memory.content[:1500]
            if memory and memory.content
            else "前章结尾节选（非摘要）：\n" + script.content[-1500:]
        )
        parts.append(f"第{row.order_index}章 {row.title}，剧本v{script.version}：\n{content}")
    return "\n\n".join(parts)


async def save_script_memory(session: AsyncSession, script: ScriptVersion, memory: str) -> None:
    if not memory.strip():
        return
    chapter = await session.get(Chapter, script.chapter_id)
    row = await session.scalar(
        select(ProjectFile).where(
            ProjectFile.project_id == script.project_id,
            ProjectFile.kind == ProjectFileKind.MEMORY,
            ProjectFile.file_metadata["script_version_id"].as_string() == script.id,
            ProjectFile.file_metadata["role"].as_string() == "chapter_continuity",
        )
    )
    if row is None:
        row = ProjectFile(
            tenant_id=script.tenant_id,
            user_id=script.user_id,
            project_id=script.project_id,
            name=f"第{chapter.order_index:03d}章-剧本v{script.version}-连续性记忆.md",
            kind=ProjectFileKind.MEMORY,
            mime_type="text/markdown",
            editable=True,
            file_metadata={
                "chapter_id": script.chapter_id,
                "script_version_id": script.id,
                "role": "chapter_continuity",
            },
        )
        session.add(row)
    row.content = memory.strip()
    row.size_bytes = len(row.content.encode("utf-8"))


async def validate_creation_capabilities(session: AsyncSession, values: dict) -> None:
    from app.db.models import AIModel

    if values.get("creation_mode") != "ai":
        return
    ratio = values.get("aspect_ratio")
    for kind in ("image", "video"):
        model = await session.get(AIModel, values.get(f"{kind}_model_id"))
        if model is None:
            raise HTTPException(status_code=422, detail=f"{kind} 模型不可用")
        capabilities = model.capabilities or {}
        ratios = capabilities.get("aspect_ratios", [])
        if ratios and ratio not in ratios:
            raise HTTPException(status_code=422, detail=f"{model.name} 不支持固定画幅 {ratio}")
        resolutions = capabilities.get("resolutions", [])
        if kind == "video" and capabilities.get("duration_resolution_map"):
            resolutions = [
                r for group in capabilities["duration_resolution_map"] for r in group["resolutions"]
            ]
        if resolutions and values.get(f"{kind}_resolution") not in resolutions:
            raise HTTPException(status_code=422, detail=f"{model.name} 不支持所选分辨率")


async def save_creation_file(
    session: AsyncSession,
    project: Project,
    name: str,
    content: str,
    kind: ProjectFileKind = ProjectFileKind.OTHER,
) -> ProjectFile:
    row = await session.scalar(
        select(ProjectFile).where(
            ProjectFile.project_id == project.id,
            ProjectFile.name == name,
        )
    )
    if row is not None and (row.file_metadata or {}).get("role") not in {"ai_creation", "ai_creation_bible"}:
        raise HTTPException(status_code=409, detail=f"项目中已有同名用户文件：{name}，请先重命名后重试")
    if row is None:
        row = ProjectFile(
            tenant_id=project.tenant_id,
            user_id=project.owner_id,
            project_id=project.id,
            name=name,
            kind=kind,
            mime_type="application/json" if name.endswith(".json") else "text/markdown",
            editable=True,
            file_metadata={"role": "ai_creation_bible" if kind == ProjectFileKind.MEMORY else "ai_creation"},
        )
        session.add(row)
    row.content = content
    row.size_bytes = len(content.encode("utf-8"))
    await session.flush()
    return row
