from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.projects import project_for_user
from app.core.config import get_settings
from app.core.security import SecretBox
from app.db.models import (
    AgentChatMessage,
    AgentChatSession,
    AgentKind,
    AgentMessageRole,
    AgentProfile,
    AIModel,
    AITask,
    Asset,
    AssetExtraction,
    AssetExtractionItem,
    AssetScope,
    AssetStatus,
    Chapter,
    ChapterStatus,
    DirectorWorkflowRun,
    DirectorWorkflowStatus,
    Handbook,
    HandbookType,
    ModelType,
    PersonalAgentAttachment,
    Project,
    ProjectFile,
    ProjectFileKind,
    Provider,
    ScriptVersion,
    StoryboardShot,
    StoryboardVersion,
    TaskEvent,
    TaskStatus,
    User,
    UserSkill,
    new_id,
)
from app.db.session import get_session
from app.domain.schemas import (
    AgentChatAttachmentPublic,
    AgentChatMessageCreate,
    AgentChatOptions,
    AgentChatRunQueuedPublic,
    AgentChatSessionCreate,
    AgentChatSessionDetail,
    AgentChatSessionPublic,
    AgentOptionPublic,
    AgentSkillPublic,
    ModelPublic,
    TaskPublic,
)
from app.services.agent_memory import retrieve_project_memories
from app.services.agent_runtime import (
    AgentRuntimeClient,
    AgentRuntimeProjectFileChange,
    AgentRuntimeProjectFileSnapshot,
)
from app.services.asset_tasks import (
    project_assets_for_generation,
    queue_asset_image_generation_tasks,
    queue_asset_prompt_generation_task,
)
from app.services.billing import resolve_task_pricing
from app.services.image_model_routing import normalize_image_resolution, resolve_image_model
from app.services.managed_skills import ensure_handbook_package, handbook_manifest
from app.services.media import ALLOWED_COVER_TYPES, MAX_COVER_BYTES, InvalidCoverImage, save_agent_chat_image
from app.services.object_storage import (
    delete_media_file,
    materialize_media_file,
    object_key_from_media_url,
    persist_media_file,
)
from app.services.task_events import publish_task_event, record_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import (
    ACTIVE_TASK_STATUSES,
    create_queued_task,
    serialize_project_task_submissions,
)

router = APIRouter(prefix="/projects/{project_id}/agent", tags=["agent-chat"])
personal_router = APIRouter(prefix="/agent", tags=["personal-agent-chat"])
MAX_SKILL_FILES = 200
MAX_SKILL_BYTES = 1_000_000
ALLOWED_PROJECT_FILE_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml"}
MAX_PROJECT_FILES = 200
MAX_PROJECT_FILE_BYTES = 2_000_000
MAX_PROJECT_FILES_TOTAL_BYTES = 12_000_000
MAX_AGENT_CREATED_FILES_PER_RUN = 50
MAX_CHAT_ATTACHMENTS = 4
SCRIPT_VERSION_COMMAND_FILE = "cineforge-script-version.json"
STORYBOARD_VERSION_COMMAND_FILE = "cineforge-storyboard-version.json"
ASSET_PROMPT_COMMAND_FILE = "cineforge-asset-prompts.json"
ASSET_IMAGE_COMMAND_FILE = "cineforge-asset-images.json"
settings = get_settings()
SCENE_AGENT_KINDS = {
    "workspace": AgentKind.GENERAL,
    "director": AgentKind.SCREENPLAY,
}


class AgentScriptVersionCommand(BaseModel):
    operation: Literal["create_script_version"]
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=1_800_000)
    review_notes: str = Field(default="", max_length=200_000)


class AgentStoryboardShotCommand(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    shot_type: str = Field(default="中景", min_length=1, max_length=80)
    duration_seconds: Decimal = Field(default=Decimal("5"), ge=1, le=300)
    scene_description: str = Field(default="", max_length=20_000)
    action_description: str = Field(default="", max_length=20_000)
    dialogue: str = Field(default="", max_length=20_000)
    image_prompt: str = Field(default="", max_length=30_000)
    video_prompt: str = Field(default="", max_length=30_000)
    asset_names: list[str] = Field(default_factory=list, max_length=100)


class AgentStoryboardVersionCommand(BaseModel):
    operation: Literal["create_storyboard_version"]
    shots: list[AgentStoryboardShotCommand] = Field(min_length=1, max_length=300)


class AgentAssetPromptCommand(BaseModel):
    operation: Literal["generate_asset_prompts"]
    target: Literal["listed_assets", "active_chapter_extraction", "all_project_assets"] = "listed_assets"
    asset_ids: list[str] = Field(default_factory=list, max_length=100)
    asset_names: list[str] = Field(default_factory=list, max_length=100)
    only_missing_prompt: bool = True
    auto_queue_images_after_prompt: bool = False


class AgentAssetImageCommand(BaseModel):
    operation: Literal["generate_asset_images"]
    target: Literal["listed_assets", "active_chapter_extraction", "all_project_assets"] = "listed_assets"
    asset_ids: list[str] = Field(default_factory=list, max_length=50)
    asset_names: list[str] = Field(default_factory=list, max_length=50)
    only_missing_image: bool = True
    auto_generate_missing_prompts: bool = True


def requested_asset_platform_action(content: str) -> Literal["prompt", "image"] | None:
    normalized = re.sub(r"\s+", "", content.lower())
    if not normalized:
        return None
    prompt_requested = any(term in normalized for term in ("提示词", "prompt"))
    image_requested = any(
        term in normalized
        for term in (
            "生图",
            "出图",
            "生成图片",
            "生成图像",
            "生成资产图",
            "资产图片",
            "定稿图",
        )
    )
    direct_image_terms = ("完成生图", "直接生图", "开始生图", "安排生图")
    if prompt_requested and not any(term in normalized for term in direct_image_terms):
        return "prompt"
    if image_requested:
        return "image"
    return None


def requested_storyboard_platform_action(content: str) -> bool:
    normalized = re.sub(r"\s+", "", content.lower())
    if not normalized:
        return False
    storyboard_requested = any(
        term in normalized
        for term in (
            "生成分镜",
            "制作分镜",
            "创建分镜",
            "做分镜",
            "出分镜",
            "分镜表",
            "镜头规划",
        )
    )
    if not storyboard_requested:
        return False
    return not any(term in normalized for term in ("审核分镜", "检查分镜", "修改分镜", "修复分镜"))


async def persist_platform_action_failure(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    chat_session: AgentChatSession,
    user_message: AgentChatMessage,
    action: str,
    title: str,
    message: str,
    now: datetime,
) -> AgentChatRunQueuedPublic:
    task = AITask(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project.id,
        task_type="agent_chat_run",
        model_id=None,
        cost=Decimal("0"),
        request_payload={
            "platform_action": action,
            "agent_chat_session_id": chat_session.id,
        },
        status=TaskStatus.FAILED,
        error_message=message,
        result_payload={
            "credit_refunded": False,
            "platform_action": action,
            "error_message": message,
        },
    )
    db.add(task)
    await db.flush()
    task.idempotency_key = task.id
    user_message.run_id = task.id
    runtime_manifest = {
        "runtime_type": "platform_error",
        "contract_version": "v1",
        "agent_chat_session_id": chat_session.id,
        "platform_action": action,
        "error_message": message,
    }
    assistant_message = AgentChatMessage(
        tenant_id=user.tenant_id,
        user_id=user.id,
        session_id=chat_session.id,
        role=AgentMessageRole.ASSISTANT,
        content=(
            f"这次没能启动{title}：{message}\n\n"
            "我已经停止本轮平台动作，没有继续空转。你可以按提示补齐前置内容后重试；"
            "如果确认前置内容已经齐备，请让管理员检查模型平台、任务队列或 Agent Runtime 日志。"
        ),
        run_id=task.id,
        finish_reason="failed",
        runtime_events=[],
        runtime_manifest=runtime_manifest,
    )
    db.add(assistant_message)
    task.result_payload = {
        **(task.result_payload or {}),
        "assistant_message_id": assistant_message.id,
    }
    event = record_task_event(
        db,
        task,
        status=TaskStatus.FAILED,
        progress=100,
        message=message,
        metadata={"platform_action": action},
    )
    if chat_session.title == "新对话":
        chat_session.title = user_message.content[:56] or title
    chat_session.runtime_manifest = runtime_manifest
    chat_session.last_message_at = now
    await db.commit()
    await db.refresh(chat_session)
    await db.refresh(user_message)
    await db.refresh(task)
    await db.refresh(event)
    await publish_task_event(task, event)
    return AgentChatRunQueuedPublic(
        session=chat_session,
        user_message=user_message,
        task=TaskPublic.model_validate(task).model_copy(
            update={
                "progress": event.progress,
                "latest_message": event.message,
                "latest_event_at": event.created_at,
            }
        ),
    )


async def inferred_asset_action_target(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    chapter: Chapter | None,
    content: str,
) -> tuple[str, list[str], list[str]]:
    assets = list(
        (
            await db.scalars(
                select(Asset)
                .where(
                    Asset.tenant_id == user.tenant_id,
                    Asset.user_id == user.id,
                    Asset.project_id == project.id,
                    Asset.scope == AssetScope.PROJECT,
                )
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )
    matched_names = [
        asset.name
        for asset in assets
        if asset.name and asset.name in content and asset.asset_type.value != "audio"
    ]
    if matched_names:
        return "listed_assets", [], list(dict.fromkeys(matched_names))
    if chapter is not None:
        return "active_chapter_extraction", [], []
    return "all_project_assets", [], []


def attachment_public(project_file: ProjectFile) -> AgentChatAttachmentPublic:
    return AgentChatAttachmentPublic(
        id=project_file.id,
        project_id=project_file.project_id,
        name=project_file.name,
        mime_type=project_file.mime_type,
        size_bytes=project_file.size_bytes,
        media_url=str(project_file.file_metadata.get("media_url") or ""),
    )


def personal_attachment_public(attachment: PersonalAgentAttachment) -> AgentChatAttachmentPublic:
    return AgentChatAttachmentPublic(
        id=attachment.id,
        project_id=None,
        name=attachment.name,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        media_url=attachment.media_url,
    )


async def message_attachments(
    db: AsyncSession,
    *,
    user: User,
    project_id: str,
    attachment_ids: list[str],
) -> list[ProjectFile]:
    if not attachment_ids:
        return []
    rows = list(
        (
            await db.scalars(
                select(ProjectFile).where(
                    ProjectFile.id.in_(attachment_ids),
                    ProjectFile.tenant_id == user.tenant_id,
                    ProjectFile.user_id == user.id,
                    ProjectFile.project_id == project_id,
                )
            )
        ).all()
    )
    by_id = {item.id: item for item in rows}
    attachments: list[ProjectFile] = []
    for attachment_id in attachment_ids:
        item = by_id.get(attachment_id)
        if (
            item is None
            or item.file_metadata.get("role") != "agent_chat_attachment"
            or item.file_metadata.get("agent_chat_message_id")
            or not item.storage_path
            or not item.mime_type.startswith("image/")
        ):
            raise HTTPException(status_code=422, detail="图片附件不存在、已被使用或不属于当前项目")
        attachments.append(item)
    return attachments


@router.post(
    "/attachments",
    response_model=AgentChatAttachmentPublic,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    project_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatAttachmentPublic:
    await project_for_user(db, project_id, user)
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_COVER_TYPES:
        await file.close()
        raise HTTPException(status_code=422, detail="仅支持 JPG、PNG 或 WebP 图片")
    data = await file.read(MAX_COVER_BYTES + 1)
    await file.close()
    if len(data) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="单张图片不能超过 8MB")

    stored_path: Path | None = None
    storage_key: str | None = None
    try:
        stored_path = await run_in_threadpool(
            save_agent_chat_image,
            data,
            uploads_root=settings.uploads_root,
            tenant_id=user.tenant_id,
            project_id=project_id,
        )
        storage_key, media_url = await persist_media_file(stored_path, "image/webp")
        original_name = Path(file.filename or "图片").name
        display_name = f"{Path(original_name).stem[:220] or '图片'}.webp"
        project_file = ProjectFile(
            id=new_id(),
            tenant_id=user.tenant_id,
            user_id=user.id,
            project_id=project_id,
            name=display_name,
            kind=ProjectFileKind.OTHER,
            mime_type="image/webp",
            size_bytes=stored_path.stat().st_size,
            storage_path=storage_key,
            editable=False,
            file_metadata={
                "role": "agent_chat_attachment",
                "media_url": media_url,
                "original_name": original_name,
                "uploaded_by": "user",
            },
        )
        db.add(project_file)
        await db.commit()
        await db.refresh(project_file)
        return attachment_public(project_file)
    except InvalidCoverImage as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        await db.rollback()
        if storage_key:
            with suppress(Exception):
                await delete_media_file(storage_key, stored_path)
        elif stored_path:
            with suppress(OSError):
                stored_path.unlink(missing_ok=True)
        raise


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    project_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    await project_for_user(db, project_id, user)
    project_file = await db.get(ProjectFile, attachment_id)
    if (
        project_file is None
        or project_file.tenant_id != user.tenant_id
        or project_file.user_id != user.id
        or project_file.project_id != project_id
        or project_file.file_metadata.get("role") != "agent_chat_attachment"
    ):
        raise HTTPException(status_code=404, detail="图片附件不存在")
    if project_file.file_metadata.get("agent_chat_message_id"):
        raise HTTPException(status_code=409, detail="已发送的图片附件不能删除")
    storage_path = project_file.storage_path
    await db.delete(project_file)
    await db.commit()
    if storage_path:
        with suppress(Exception):
            await delete_media_file(storage_path)


async def session_for_user(
    db: AsyncSession,
    project_id: str,
    session_id: str,
    user: User,
) -> AgentChatSession:
    chat_session = await db.get(AgentChatSession, session_id)
    if (
        chat_session is None
        or chat_session.tenant_id != user.tenant_id
        or chat_session.user_id != user.id
        or chat_session.project_id != project_id
    ):
        raise HTTPException(status_code=404, detail="Agent 会话不存在")
    return chat_session


async def enabled_agent(db: AsyncSession, agent_id: str, tenant_id: str) -> AgentProfile:
    agent = await db.get(AgentProfile, agent_id)
    if agent is None or agent.tenant_id != tenant_id or not agent.enabled:
        raise HTTPException(status_code=422, detail="所选 Agent 当前不可用")
    return agent


async def resolve_scene_agent(
    db: AsyncSession,
    *,
    tenant_id: str,
    scene: str,
) -> AgentProfile:
    agent_kind = SCENE_AGENT_KINDS.get(scene)
    if agent_kind is None:
        raise HTTPException(status_code=422, detail="不支持的 Agent 调用场景")
    agents = list(
        (
            await db.scalars(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == tenant_id,
                    AgentProfile.kind == agent_kind,
                    AgentProfile.enabled.is_(True),
                )
            )
        ).all()
    )
    if not agents:
        label = "剧本 Agent" if agent_kind == AgentKind.SCREENPLAY else "通用 AI"
        raise HTTPException(status_code=409, detail=f"管理员尚未配置可用的{label}")

    def routing_key(agent: AgentProfile) -> tuple[int, str, str]:
        raw_priority = (agent.config or {}).get("routing_priority", 0)
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            priority = 0
        return (-priority, agent.name, agent.id)

    return min(agents, key=routing_key)


async def chat_session_scene(db: AsyncSession, chat_session: AgentChatSession) -> str:
    scene = str((chat_session.runtime_manifest or {}).get("scene") or "")
    if scene in SCENE_AGENT_KINDS:
        return scene
    agent = await db.get(AgentProfile, chat_session.agent_profile_id)
    if agent is not None and agent.kind == AgentKind.SCREENPLAY:
        return "director"
    return "workspace"


async def resolve_text_model(
    db: AsyncSession,
    agent: AgentProfile,
    tenant_id: str,
    model_id: str | None = None,
) -> tuple[AIModel, Provider, str]:
    model = await db.get(AIModel, model_id) if model_id else None
    if model is None and not model_id:
        model = await db.get(AIModel, agent.text_model_id) if agent.text_model_id else None
    if model is None:
        model = await db.scalar(
            select(AIModel).where(
                AIModel.tenant_id == tenant_id,
                AIModel.model_type == ModelType.TEXT,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if (
        model is None
        or model.tenant_id != tenant_id
        or model.model_type != ModelType.TEXT
        or not model.enabled
    ):
        raise HTTPException(status_code=409, detail="当前 Agent 没有可用的文本模型")

    provider = await db.get(Provider, model.provider_id)
    if provider is None or provider.tenant_id != tenant_id or not provider.enabled:
        raise HTTPException(status_code=409, detail="文本模型所属平台当前不可用")
    api_key = SecretBox().decrypt(provider.encrypted_api_key)
    if not api_key:
        raise HTTPException(status_code=409, detail="文本模型平台尚未配置 API Key")
    return model, provider, api_key


async def project_handbooks(db: AsyncSession, project: Project) -> list[Handbook]:
    handbooks: list[Handbook] = []
    bindings = (
        (project.visual_handbook_id, HandbookType.VISUAL, "视觉手册"),
        (project.director_handbook_id, HandbookType.DIRECTOR, "导演手册"),
    )
    for handbook_id, expected_type, label in bindings:
        if not handbook_id:
            raise HTTPException(status_code=409, detail=f"当前项目尚未选择{label}")
        handbook = await db.get(Handbook, handbook_id)
        if (
            handbook is None
            or handbook.tenant_id != project.tenant_id
            or handbook.handbook_type != expected_type
            or not handbook.enabled
        ):
            raise HTTPException(status_code=409, detail=f"当前项目选择的{label}不可用，请重新配置")
        handbooks.append(handbook)
    return handbooks


def skill_snapshots(handbooks: list[Handbook]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    root = settings.skills_root.resolve()
    snapshots: list[dict[str, Any]] = []
    versions: dict[str, str] = {}
    for handbook in handbooks:
        ensure_handbook_package(handbook)
        handbook_root = (root / handbook.skill_path).resolve()
        if not handbook_root.is_relative_to(root) or not handbook_root.is_dir():
            raise HTTPException(status_code=409, detail=f"创作手册《{handbook.name}》的技能目录不可用")
        versions[handbook.skill_path] = str(handbook.version)
        for item in handbook_manifest(handbook.handbook_type):
            file_path = handbook_root / item.filename
            if file_path.is_symlink() or not file_path.is_file():
                raise HTTPException(
                    status_code=409,
                    detail=f"创作手册《{handbook.name}》缺少固定文件：{item.filename}",
                )
            if len(snapshots) >= MAX_SKILL_FILES:
                raise HTTPException(status_code=422, detail="项目 Skills 文件数量超过运行上限")
            if file_path.stat().st_size > MAX_SKILL_BYTES:
                raise HTTPException(status_code=422, detail=f"Skills 文件过大：{file_path.name}")
            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail=f"Skills 文件不是 UTF-8：{file_path.name}"
                ) from exc
            if not content.strip():
                raise HTTPException(
                    status_code=409,
                    detail=f"创作手册《{handbook.name}》存在空文件：{item.filename}",
                )
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            relative_path = file_path.relative_to(root).as_posix()
            snapshots.append(
                {
                    "path": relative_path,
                    "content": content,
                    "sha256": digest,
                    "version": f"{handbook.version}:{digest[:12]}",
                }
            )
    return snapshots, versions


async def memory_context(
    db: AsyncSession,
    user: User,
    project_id: str | None,
    query: str = "",
) -> list[str]:
    memories = await retrieve_project_memories(
        db,
        user=user,
        project_id=project_id,
        query=query,
    )
    return [item.as_context() for item in memories]


async def active_chat_task(
    db: AsyncSession,
    *,
    user: User,
    project_id: str | None,
    chat_session_id: str,
) -> AITask | None:
    tasks = list(
        (
            await db.scalars(
                select(AITask)
                .where(
                    AITask.tenant_id == user.tenant_id,
                    AITask.user_id == user.id,
                    AITask.project_id == project_id,
                    AITask.task_type == "agent_chat_run",
                    AITask.status.in_(ACTIVE_TASK_STATUSES),
                )
                .order_by(AITask.created_at.desc())
            )
        ).all()
    )
    return next(
        (task for task in tasks if task.request_payload.get("agent_chat_session_id") == chat_session_id),
        None,
    )


def snapshot_filename(name: str) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "project-file"
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_PROJECT_FILE_EXTENSIONS:
        suffix = ".txt"
        stem = safe_name
    else:
        stem = safe_name[: -len(suffix)]
    return f"{stem[: 255 - len(suffix)]}{suffix}"


async def project_file_snapshots(
    db: AsyncSession,
    user: User,
    project_id: str,
) -> list[AgentRuntimeProjectFileSnapshot]:
    files = list(
        (
            await db.scalars(
                select(ProjectFile)
                .where(
                    ProjectFile.tenant_id == user.tenant_id,
                    ProjectFile.user_id == user.id,
                    ProjectFile.project_id == project_id,
                    ProjectFile.content.is_not(None),
                )
                .order_by(ProjectFile.updated_at.desc())
            )
        ).all()
    )
    if len(files) > MAX_PROJECT_FILES:
        raise HTTPException(status_code=422, detail="项目文本文件数量超过 Agent 运行上限")

    snapshots: list[AgentRuntimeProjectFileSnapshot] = []
    total_bytes = 0
    for item in files:
        content = item.content or ""
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_PROJECT_FILE_BYTES:
            raise HTTPException(status_code=422, detail=f"项目文件过大：{item.name}")
        total_bytes += content_bytes
        if total_bytes > MAX_PROJECT_FILES_TOTAL_BYTES:
            raise HTTPException(status_code=422, detail="项目文件总大小超过 Agent 运行上限")
        filename = snapshot_filename(item.name)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        snapshots.append(
            AgentRuntimeProjectFileSnapshot(
                id=item.id,
                name=item.name,
                kind=item.kind.value,
                path=f"project-files/{item.id}/{filename}",
                content=content,
                sha256=digest,
                editable=item.editable,
            )
        )
    return snapshots


def project_file_mime_type(name: str) -> str:
    return {
        ".md": "text/markdown",
        ".json": "application/json",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }.get(Path(name).suffix.lower(), "text/plain")


def rejected_file_change(
    operation: str,
    name: str,
    reason: str,
    *,
    status_value: str = "rejected",
    file_id: str | None = None,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "file_id": file_id,
        "name": name,
        "status": status_value,
        "reason": reason,
    }


async def apply_script_version_command(
    db: AsyncSession,
    user: User,
    project: Project,
    chapter: Chapter | None,
    run_id: str,
    raw_content: str | None,
) -> dict[str, Any]:
    if chapter is None:
        return rejected_file_change(
            "publish_script_version",
            SCRIPT_VERSION_COMMAND_FILE,
            "当前对话没有绑定导演台章节",
        )
    try:
        command = AgentScriptVersionCommand.model_validate(json.loads(raw_content or ""))
    except (json.JSONDecodeError, ValidationError) as exc:
        return rejected_file_change(
            "publish_script_version",
            SCRIPT_VERSION_COMMAND_FILE,
            f"正式剧本指令格式无效：{type(exc).__name__}",
        )

    active_workflow = await db.scalar(
        select(DirectorWorkflowRun).where(
            DirectorWorkflowRun.chapter_id == chapter.id,
            DirectorWorkflowRun.user_id == user.id,
            DirectorWorkflowRun.status.in_(
                {DirectorWorkflowStatus.RUNNING, DirectorWorkflowStatus.WAITING_USER}
            ),
        )
    )
    if active_workflow is not None:
        return rejected_file_change(
            "publish_script_version",
            SCRIPT_VERSION_COMMAND_FILE,
            "当前章节已有导演审核或待确认流程，请先等待审核完成或处理审核选择",
            status_value="conflict",
        )

    latest = await db.scalar(
        select(func.max(ScriptVersion.version)).where(ScriptVersion.chapter_id == chapter.id)
    )
    script = ScriptVersion(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project.id,
        chapter_id=chapter.id,
        version=(latest or 0) + 1,
        title=command.title,
        content=command.content,
        status="reviewing",
        review_notes=command.review_notes,
        is_active=False,
    )
    db.add(script)
    await db.flush()
    file_content = (
        f"# {command.title}\n\n{command.content}\n\n"
        f"## 审核关注点\n\n{command.review_notes or '无'}\n"
    )
    project_file = ProjectFile(
        id=new_id(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project.id,
        name=f"{chapter.title[:210]}-AI剧本-v{script.version}.md",
        kind=ProjectFileKind.SCRIPT,
        mime_type="text/markdown",
        size_bytes=len(file_content.encode("utf-8")),
        content=file_content,
        editable=True,
        file_metadata={
            "chapter_id": chapter.id,
            "script_version_id": script.id,
            "source_task_id": run_id,
            "created_by": "agent",
        },
    )
    db.add(project_file)
    chapter.status = ChapterStatus.REVIEWING
    return {
        "operation": "publish_script_version",
        "file_id": project_file.id,
        "name": command.title,
        "status": "applied",
        "reason": None,
        "resource_type": "script_version",
        "resource_id": script.id,
        "chapter_id": chapter.id,
        "version": script.version,
    }


def _split_markdown_row(row: str) -> list[str]:
    stripped = row.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped.strip("|"):
        if character == "\\" and not escaped:
            escaped = True
            current.append(character)
            continue
        if character == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
            continue
        escaped = False
        current.append(character)
    cells.append("".join(current).strip())
    return cells


def _clean_markdown_cell(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\*\*([^*]*)\*\*", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return value.replace("\\|", "|").strip()


def _duration_seconds(value: str) -> Decimal:
    match = re.search(r"(\d+(?:\.\d+)?)", value)
    if not match:
        return Decimal("5")
    duration = Decimal(match.group(1))
    return min(max(duration, Decimal("1")), Decimal("300"))


def _asset_names(value: str) -> list[str]:
    if not value or value in {"同上", "同场", "全体", "全场角色"}:
        return []
    names = [
        item.strip(" 　，,;；")
        for item in re.split(r"[、,，;；\n]+", value)
        if item.strip(" 　，,;；")
    ]
    return list(dict.fromkeys(names))


def parse_storyboard_markdown_table(raw_content: str) -> list[AgentStoryboardShotCommand]:
    rows = [line for line in raw_content.splitlines() if line.strip().startswith("|")]
    for index in range(len(rows) - 2):
        headers = [_clean_markdown_cell(item) for item in _split_markdown_row(rows[index])]
        separator = _split_markdown_row(rows[index + 1])
        valid_separator = all(re.fullmatch(r":?-{3,}:?", item.strip()) for item in separator)
        if not headers or not separator or not valid_separator:
            continue
        normalized = [re.sub(r"\s+", "", item) for item in headers]
        if "镜号" not in normalized or not any("画面" in item or "动作" in item for item in normalized):
            continue
        shots: list[AgentStoryboardShotCommand] = []
        for row in rows[index + 2 :]:
            cells = [_clean_markdown_cell(item) for item in _split_markdown_row(row)]
            if len(cells) != len(headers):
                if shots:
                    break
                continue
            data = dict(zip(normalized, cells, strict=True))
            shot_number = data.get("镜号") or data.get("序号") or str(len(shots) + 1).zfill(3)
            scene = data.get("场次") or data.get("场景") or ""
            framing = data.get("景别/机位") or data.get("景别") or "中景"
            camera = data.get("运镜") or ""
            action = (
                data.get("画面、动作与表演")
                or data.get("画面动作与表演")
                or data.get("画面描述")
                or data.get("动作")
                or ""
            )
            dialogue = data.get("台词/声音") or data.get("台词") or ""
            assets = data.get("资产") or data.get("关联资产名称") or ""
            light = data.get("光影/转场") or data.get("光影氛围") or ""
            title = f"镜头 {shot_number}"
            if scene:
                title = f"{title} · 场{scene}"
            scene_description = "；".join(item for item in (f"场次：{scene}" if scene else "", light) if item)
            action_description = "\n".join(
                item for item in (action, f"运镜：{camera}" if camera else "") if item
            )
            image_prompt = "，".join(
                item
                for item in (
                    framing,
                    action,
                    light,
                    f"关联资产：{assets}" if assets else "",
                )
                if item
            )
            video_prompt = "，".join(
                item
                for item in (
                    f"时长 {data.get('时长') or '5s'}",
                    f"运镜：{camera}" if camera else "",
                    action,
                    f"声音/台词：{dialogue}" if dialogue else "",
                )
                if item
            )
            shots.append(
                AgentStoryboardShotCommand(
                    title=title,
                    shot_type=framing,
                    duration_seconds=_duration_seconds(data.get("时长") or ""),
                    scene_description=scene_description,
                    action_description=action_description,
                    dialogue=dialogue,
                    image_prompt=image_prompt or action,
                    video_prompt=video_prompt or action,
                    asset_names=_asset_names(assets),
                )
            )
        if shots:
            return shots
    return []


async def _storyboard_ready_assets(
    db: AsyncSession,
    extraction: AssetExtraction,
) -> tuple[list[Asset], list[str]]:
    assets = list(
        (
            await db.scalars(
                select(Asset)
                .join(AssetExtractionItem, AssetExtractionItem.asset_id == Asset.id)
                .where(
                    AssetExtractionItem.extraction_id == extraction.id,
                    Asset.user_id == extraction.user_id,
                )
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )
    missing: list[str] = []
    for asset in assets:
        if asset.asset_type.value == "audio":
            continue
        key = object_key_from_media_url(asset.media_url)
        if asset.status != AssetStatus.READY or not key:
            missing.append(asset.name)
            continue
        try:
            await materialize_media_file(key)
        except (FileNotFoundError, ValueError):
            missing.append(asset.name)
    return assets, missing


async def apply_storyboard_version_command(
    db: AsyncSession,
    user: User,
    project: Project,
    chapter: Chapter | None,
    run_id: str,
    raw_content: str | None,
    *,
    source_name: str = STORYBOARD_VERSION_COMMAND_FILE,
) -> dict[str, Any]:
    if chapter is None:
        return rejected_file_change(
            "publish_storyboard_version",
            source_name,
            "当前对话没有绑定导演台章节",
        )
    try:
        command = AgentStoryboardVersionCommand.model_validate(json.loads(raw_content or ""))
        shots = command.shots
    except (json.JSONDecodeError, ValidationError):
        shots = parse_storyboard_markdown_table(raw_content or "")
        if not shots:
            return rejected_file_change(
                "publish_storyboard_version",
                source_name,
                "正式分镜指令格式无效，且未识别到可导入的分镜表",
            )

    if not chapter.active_script_version_id:
        return rejected_file_change("publish_storyboard_version", source_name, "生成分镜前必须先选择生效剧本")
    script = await db.get(ScriptVersion, chapter.active_script_version_id)
    if script is None or script.chapter_id != chapter.id or script.user_id != user.id:
        return rejected_file_change("publish_storyboard_version", source_name, "章节生效剧本不可用")
    extraction = await db.scalar(
        select(AssetExtraction).where(
            AssetExtraction.chapter_id == chapter.id,
            AssetExtraction.script_version_id == script.id,
            AssetExtraction.user_id == user.id,
            AssetExtraction.is_active.is_(True),
        )
    )
    if extraction is None:
        return rejected_file_change(
            "publish_storyboard_version",
            source_name,
            "生成分镜前必须先完成当前剧本的资产提取",
        )
    assets, missing = await _storyboard_ready_assets(db, extraction)
    if missing:
        return rejected_file_change(
            "publish_storyboard_version",
            source_name,
            f"生成分镜前必须先完成资产图片：{'、'.join(missing[:8])}",
            status_value="conflict",
        )

    asset_by_name = {asset.name: asset for asset in assets}
    strict_command = source_name == STORYBOARD_VERSION_COMMAND_FILE
    unknown = sorted({name for shot in shots for name in shot.asset_names if name not in asset_by_name})
    if unknown and strict_command:
        return rejected_file_change(
            "publish_storyboard_version",
            source_name,
            f"正式分镜引用了未知资产：{'、'.join(unknown[:8])}",
        )

    latest = await db.scalar(
        select(func.max(StoryboardVersion.version)).where(StoryboardVersion.chapter_id == chapter.id)
    )
    await db.execute(
        StoryboardVersion.__table__.update()
        .where(StoryboardVersion.chapter_id == chapter.id, StoryboardVersion.is_active.is_(True))
        .values(is_active=False, invalidated_reason="Agent 已发布新的分镜版本")
    )
    storyboard = StoryboardVersion(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project.id,
        chapter_id=chapter.id,
        script_version_id=script.id,
        version=(latest or 0) + 1,
        content=[],
        is_active=True,
    )
    db.add(storyboard)
    await db.flush()
    content: list[dict[str, Any]] = []
    records: list[StoryboardShot] = []
    for index, payload in enumerate(shots, start=1):
        linked_assets = [asset_by_name[name] for name in payload.asset_names if name in asset_by_name]
        asset_ids = [asset.id for asset in linked_assets]
        reference_url = next((asset.media_url for asset in linked_assets if asset.media_url), None)
        values = payload.model_dump(mode="json", exclude={"asset_names"})
        shot = StoryboardShot(
            tenant_id=user.tenant_id,
            user_id=user.id,
            project_id=project.id,
            chapter_id=chapter.id,
            storyboard_version_id=storyboard.id,
            order_index=index,
            **values,
            asset_ids=asset_ids,
            reference_image_url=reference_url,
        )
        db.add(shot)
        await db.flush()
        records.append(shot)
        content.append({"order_index": index, **payload.model_dump(mode="json"), "asset_ids": asset_ids})
    storyboard.content = content
    serialized = json.dumps({"shots": content}, ensure_ascii=False, indent=2)
    project_file = ProjectFile(
        id=new_id(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project.id,
        name=f"{chapter.title[:210]}-分镜表-v{storyboard.version}.json",
        kind=ProjectFileKind.STORYBOARD,
        mime_type="application/json",
        size_bytes=len(serialized.encode("utf-8")),
        content=serialized,
        editable=True,
        file_metadata={
            "chapter_id": chapter.id,
            "script_version_id": script.id,
            "storyboard_version_id": storyboard.id,
            "source_task_id": run_id,
            "created_by": "agent",
            "source_name": source_name,
            "unmatched_asset_names": unknown,
        },
    )
    db.add(project_file)
    chapter.status = ChapterStatus.STORYBOARD
    return {
        "operation": "publish_storyboard_version",
        "file_id": project_file.id,
        "name": f"分镜 v{storyboard.version}",
        "status": "applied",
        "reason": None,
        "resource_type": "storyboard_version",
        "resource_id": storyboard.id,
        "chapter_id": chapter.id,
        "script_version_id": script.id,
        "version": storyboard.version,
        "shot_count": len(records),
    }


async def apply_asset_prompt_command(
    db: AsyncSession,
    user: User,
    project: Project,
    chapter: Chapter | None,
    raw_content: str | None,
    *,
    dispatches: list[tuple[AITask, TaskEvent]] | None = None,
) -> dict[str, Any]:
    try:
        command = AgentAssetPromptCommand.model_validate(json.loads(raw_content or ""))
    except (json.JSONDecodeError, ValidationError) as exc:
        return rejected_file_change(
            "queue_asset_prompt_generation",
            ASSET_PROMPT_COMMAND_FILE,
            f"资产提示词任务指令格式无效：{type(exc).__name__}",
        )
    try:
        assets = await project_assets_for_generation(
            db,
            user=user,
            project=project,
            target=command.target,
            asset_ids=command.asset_ids,
            asset_names=command.asset_names,
            chapter_id=chapter.id if chapter else None,
        )
        task, event, selected_assets = await queue_asset_prompt_generation_task(
            db,
            user=user,
            project=project,
            assets=assets,
            only_missing_prompt=command.only_missing_prompt,
            auto_queue_images_after_prompt=command.auto_queue_images_after_prompt,
        )
    except HTTPException as exc:
        return rejected_file_change(
            "queue_asset_prompt_generation",
            ASSET_PROMPT_COMMAND_FILE,
            str(exc.detail),
            status_value="conflict" if exc.status_code in {402, 409, 422} else "rejected",
        )
    if dispatches is not None:
        dispatches.append((task, event))
    return {
        "operation": "queue_asset_prompt_generation",
        "file_id": None,
        "name": f"{len(selected_assets)} 个资产提示词",
        "status": "applied",
        "reason": None,
        "resource_type": "asset_prompt_task",
        "resource_id": task.id,
        "task_id": task.id,
        "asset_ids": [asset.id for asset in selected_assets],
        "asset_count": len(selected_assets),
        "auto_queue_images_after_prompt": command.auto_queue_images_after_prompt,
    }


async def apply_asset_image_command(
    db: AsyncSession,
    user: User,
    project: Project,
    chapter: Chapter | None,
    raw_content: str | None,
    *,
    dispatches: list[tuple[AITask, TaskEvent]] | None = None,
) -> dict[str, Any]:
    try:
        command = AgentAssetImageCommand.model_validate(json.loads(raw_content or ""))
    except (json.JSONDecodeError, ValidationError) as exc:
        return rejected_file_change(
            "queue_asset_image_generation",
            ASSET_IMAGE_COMMAND_FILE,
            f"资产生图任务指令格式无效：{type(exc).__name__}",
        )
    try:
        assets = await project_assets_for_generation(
            db,
            user=user,
            project=project,
            target=command.target,
            asset_ids=command.asset_ids,
            asset_names=command.asset_names,
            chapter_id=chapter.id if chapter else None,
        )
        non_audio_assets = [asset for asset in assets if asset.asset_type.value != "audio"]
        missing_prompt_assets = [asset for asset in non_audio_assets if not asset.generation_prompt.strip()]
        ready_for_image = [asset for asset in non_audio_assets if asset.generation_prompt.strip()]
        prompt_task: AITask | None = None
        prompt_assets: list[Asset] = []
        image_queue_error: str | None = None
        if missing_prompt_assets:
            if not command.auto_generate_missing_prompts:
                missing_names = "、".join(asset.name for asset in missing_prompt_assets[:8])
                return rejected_file_change(
                    "queue_asset_image_generation",
                    ASSET_IMAGE_COMMAND_FILE,
                    f"部分资产缺少生图提示词：{missing_names}",
                    status_value="conflict",
                )
            prompt_task, prompt_event, prompt_assets = await queue_asset_prompt_generation_task(
                db,
                user=user,
                project=project,
                assets=missing_prompt_assets,
                only_missing_prompt=True,
                auto_queue_images_after_prompt=True,
            )
            if dispatches is not None:
                dispatches.append((prompt_task, prompt_event))
        image_queued = []
        if ready_for_image:
            try:
                image_queued = await queue_asset_image_generation_tasks(
                    db,
                    user=user,
                    project=project,
                    assets=ready_for_image,
                    only_missing_image=command.only_missing_image,
                )
            except HTTPException as exc:
                if prompt_task is None:
                    raise
                image_queue_error = str(exc.detail)
        if prompt_task is None and not image_queued:
            return rejected_file_change(
                "queue_asset_image_generation",
                ASSET_IMAGE_COMMAND_FILE,
                image_queue_error or "没有可用于生图的非音频资产",
                status_value="conflict",
            )
    except HTTPException as exc:
        return rejected_file_change(
            "queue_asset_image_generation",
            ASSET_IMAGE_COMMAND_FILE,
            str(exc.detail),
            status_value="conflict" if exc.status_code in {402, 409, 422} else "rejected",
        )
    if dispatches is not None:
        dispatches.extend((task, event) for task, event, _asset in image_queued)
    image_tasks = [task for task, _event, _asset in image_queued]
    image_assets = [asset for _task, _event, asset in image_queued]
    all_task_ids = [task.id for task in image_tasks]
    if prompt_task is not None:
        all_task_ids.insert(0, prompt_task.id)
    resource_id = (
        prompt_task.id
        if prompt_task and not image_tasks
        else (image_tasks[0].id if image_tasks else None)
    )
    return {
        "operation": "queue_asset_image_generation",
        "file_id": None,
        "name": "资产生图任务",
        "status": "applied",
        "reason": None,
        "resource_type": "asset_image_tasks",
        "resource_id": resource_id,
        "task_ids": all_task_ids,
        "asset_ids": [asset.id for asset in image_assets],
        "asset_count": len(image_assets),
        "prompt_task_id": prompt_task.id if prompt_task else None,
        "prompt_asset_ids": [asset.id for asset in prompt_assets],
        "prompt_asset_count": len(prompt_assets),
        "image_queue_error": image_queue_error,
        "auto_generate_missing_prompts": command.auto_generate_missing_prompts,
    }


async def apply_project_file_changes(
    db: AsyncSession,
    user: User,
    project: Project,
    chat_session: AgentChatSession,
    run_id: str,
    snapshots: list[AgentRuntimeProjectFileSnapshot],
    changes: list[AgentRuntimeProjectFileChange],
    chapter: Chapter | None = None,
    task_dispatches: list[tuple[AITask, TaskEvent]] | None = None,
) -> list[dict[str, Any]]:
    snapshot_by_id = {item.id: item for item in snapshots}
    outcomes: list[dict[str, Any]] = []
    seen_file_ids: set[str] = set()
    created_count = 0
    file_count = int(
        await db.scalar(
            select(func.count(ProjectFile.id)).where(
                ProjectFile.tenant_id == user.tenant_id,
                ProjectFile.user_id == user.id,
                ProjectFile.project_id == project.id,
                ProjectFile.content.is_not(None),
            )
        )
        or 0
    )

    for change in changes[:MAX_PROJECT_FILES]:
        operation = change.operation
        display_name = change.name or change.file_id or "未命名文件"
        if operation == "create":
            created_count += 1
            content = change.content
            name = (change.name or "").strip()
            if created_count > MAX_AGENT_CREATED_FILES_PER_RUN:
                outcomes.append(rejected_file_change(operation, display_name, "单次新建文件数量超过上限"))
                continue
            if name == SCRIPT_VERSION_COMMAND_FILE:
                if file_count >= MAX_PROJECT_FILES:
                    outcomes.append(
                        rejected_file_change(operation, name, "项目文件数量已达上限")
                    )
                    continue
                outcome = await apply_script_version_command(
                    db,
                    user,
                    project,
                    chapter,
                    run_id,
                    content,
                )
                outcomes.append(outcome)
                if outcome["status"] == "applied":
                    file_count += 1
                continue
            if name == STORYBOARD_VERSION_COMMAND_FILE:
                if file_count >= MAX_PROJECT_FILES:
                    outcomes.append(
                        rejected_file_change(operation, name, "项目文件数量已达上限")
                    )
                    continue
                outcome = await apply_storyboard_version_command(
                    db,
                    user,
                    project,
                    chapter,
                    run_id,
                    content,
                )
                outcomes.append(outcome)
                if outcome["status"] == "applied":
                    file_count += 1
                continue
            if name == ASSET_PROMPT_COMMAND_FILE:
                outcome = await apply_asset_prompt_command(
                    db,
                    user,
                    project,
                    chapter,
                    content,
                    dispatches=task_dispatches,
                )
                outcomes.append(outcome)
                continue
            if name == ASSET_IMAGE_COMMAND_FILE:
                outcome = await apply_asset_image_command(
                    db,
                    user,
                    project,
                    chapter,
                    content,
                    dispatches=task_dispatches,
                )
                outcomes.append(outcome)
                continue
            if "分镜" in name and Path(name).suffix.lower() in {".md", ".txt"}:
                outcome = await apply_storyboard_version_command(
                    db,
                    user,
                    project,
                    chapter,
                    run_id,
                    content,
                    source_name=name,
                )
                outcomes.append(outcome)
                if outcome["status"] == "applied":
                    file_count += 1
                continue
            if (
                not name
                or "/" in name
                or "\\" in name
                or Path(name).suffix.lower() not in ALLOWED_PROJECT_FILE_EXTENSIONS
            ):
                outcomes.append(rejected_file_change(operation, display_name, "文件名或文件类型不受支持"))
                continue
            if content is None or len(content.encode("utf-8")) > MAX_PROJECT_FILE_BYTES:
                outcomes.append(rejected_file_change(operation, name, "文件内容缺失或超过大小上限"))
                continue
            if file_count >= MAX_PROJECT_FILES:
                outcomes.append(rejected_file_change(operation, name, "项目文件数量已达上限"))
                continue
            project_file = ProjectFile(
                id=new_id(),
                tenant_id=user.tenant_id,
                user_id=user.id,
                project_id=project.id,
                name=name,
                kind=ProjectFileKind.OTHER,
                mime_type=project_file_mime_type(name),
                size_bytes=len(content.encode("utf-8")),
                content=content,
                editable=True,
                file_metadata={
                    "created_by": "agent",
                    "last_agent_run_id": run_id,
                    "agent_session_id": chat_session.id,
                },
            )
            db.add(project_file)
            file_count += 1
            outcomes.append(
                {
                    "operation": operation,
                    "file_id": project_file.id,
                    "name": name,
                    "status": "applied",
                    "reason": None,
                }
            )
            continue

        file_id = change.file_id or ""
        snapshot = snapshot_by_id.get(file_id)
        if operation not in {"update", "delete"} or snapshot is None:
            outcomes.append(
                rejected_file_change(
                    operation,
                    display_name,
                    "文件操作不在本次快照范围内",
                    file_id=file_id or None,
                )
            )
            continue
        if file_id in seen_file_ids:
            outcomes.append(
                rejected_file_change(operation, snapshot.name, "同一文件存在重复操作", file_id=file_id)
            )
            continue
        seen_file_ids.add(file_id)
        if change.base_sha256 != snapshot.sha256:
            outcomes.append(
                rejected_file_change(operation, snapshot.name, "运行时基准版本无效", file_id=file_id)
            )
            continue

        current = await db.scalar(
            select(ProjectFile)
            .where(
                ProjectFile.id == file_id,
                ProjectFile.tenant_id == user.tenant_id,
                ProjectFile.user_id == user.id,
                ProjectFile.project_id == project.id,
            )
            .with_for_update()
        )
        if current is None:
            outcomes.append(
                rejected_file_change(
                    operation,
                    snapshot.name,
                    "文件在 Agent 运行期间已被删除",
                    status_value="conflict",
                    file_id=file_id,
                )
            )
            continue
        current_digest = hashlib.sha256((current.content or "").encode("utf-8")).hexdigest()
        if current_digest != snapshot.sha256:
            outcomes.append(
                rejected_file_change(
                    operation,
                    current.name,
                    "文件在 Agent 运行期间已被修改",
                    status_value="conflict",
                    file_id=file_id,
                )
            )
            continue
        if not current.editable:
            outcomes.append(
                rejected_file_change(operation, current.name, "该文件不可由 Agent 修改", file_id=file_id)
            )
            continue
        if operation == "delete":
            if current.kind == ProjectFileKind.SOURCE:
                outcomes.append(
                    rejected_file_change(operation, current.name, "Agent 不允许删除来源文件", file_id=file_id)
                )
                continue
            await db.delete(current)
            outcomes.append(
                {
                    "operation": operation,
                    "file_id": file_id,
                    "name": current.name,
                    "status": "applied",
                    "reason": None,
                }
            )
            file_count -= 1
            continue

        content = change.content
        if content is None or len(content.encode("utf-8")) > MAX_PROJECT_FILE_BYTES:
            outcomes.append(
                rejected_file_change(operation, current.name, "文件内容缺失或超过大小上限", file_id=file_id)
            )
            continue
        current.content = content
        current.size_bytes = len(content.encode("utf-8"))
        metadata = dict(current.file_metadata or {})
        metadata.update(
            {
                "last_agent_run_id": run_id,
                "agent_session_id": chat_session.id,
                "last_modified_by": "agent",
            }
        )
        current.file_metadata = metadata
        outcomes.append(
            {
                "operation": operation,
                "file_id": file_id,
                "name": current.name,
                "status": "applied",
                "reason": None,
            }
        )

    if len(changes) > MAX_PROJECT_FILES:
        outcomes.append(rejected_file_change("batch", "其余文件操作", "返回的文件操作数量超过上限"))
    return outcomes


@router.get("/options", response_model=AgentChatOptions)
async def chat_options(
    project_id: str,
    scene: Literal["workspace", "director"] = "workspace",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatOptions:
    project = await project_for_user(db, project_id, user)
    agent = await resolve_scene_agent(db, tenant_id=user.tenant_id, scene=scene)
    handbooks = await project_handbooks(db, project)
    return AgentChatOptions(
        agents=[AgentOptionPublic.model_validate(agent)],
        skills=[
            AgentSkillPublic(
                id=item.id,
                handbook_type=item.handbook_type,
                name=item.name,
                description=item.description,
                version=item.version,
            )
            for item in handbooks
        ],
    )


@router.get("/sessions", response_model=list[AgentChatSessionPublic])
async def list_sessions(
    project_id: str,
    scene: Literal["workspace", "director"] = "workspace",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[AgentChatSession]:
    await project_for_user(db, project_id, user)
    agent_kind = SCENE_AGENT_KINDS[scene]
    return list(
        (
            await db.scalars(
                select(AgentChatSession)
                .join(AgentProfile, AgentProfile.id == AgentChatSession.agent_profile_id)
                .where(
                    AgentChatSession.tenant_id == user.tenant_id,
                    AgentChatSession.user_id == user.id,
                    AgentChatSession.project_id == project_id,
                    AgentProfile.kind == agent_kind,
                )
                .order_by(AgentChatSession.last_message_at.desc())
                .limit(30)
            )
        ).all()
    )


@router.post("/sessions", response_model=AgentChatSessionPublic, status_code=status.HTTP_201_CREATED)
async def create_session(
    project_id: str,
    payload: AgentChatSessionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatSession:
    await project_for_user(db, project_id, user)
    agent = await resolve_scene_agent(db, tenant_id=user.tenant_id, scene=payload.scene)
    chat_session = AgentChatSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        agent_profile_id=agent.id,
        runtime_manifest={"scene": payload.scene},
    )
    db.add(chat_session)
    await db.commit()
    await db.refresh(chat_session)
    return chat_session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    await project_for_user(db, project_id, user)
    chat_session = await session_for_user(db, project_id, session_id, user)
    pending = await active_chat_task(
        db,
        user=user,
        project_id=project_id,
        chat_session_id=chat_session.id,
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="当前对话仍在生成，请先停止后再删除")

    project_files = list(
        (
            await db.scalars(
                select(ProjectFile).where(
                    ProjectFile.tenant_id == user.tenant_id,
                    ProjectFile.user_id == user.id,
                    ProjectFile.project_id == project_id,
                )
            )
        ).all()
    )
    attachments = [
        item
        for item in project_files
        if item.file_metadata.get("role") == "agent_chat_attachment"
        and item.file_metadata.get("agent_chat_session_id") == chat_session.id
    ]
    storage_items = [(item.storage_path, item) for item in attachments if item.storage_path]
    await db.execute(delete(AgentChatMessage).where(AgentChatMessage.session_id == chat_session.id))
    for attachment in attachments:
        await db.delete(attachment)
    await db.execute(delete(AgentChatSession).where(AgentChatSession.id == chat_session.id))
    await db.commit()

    runtime = AgentRuntimeClient(
        settings.agent_runtime_url,
        settings.agent_runtime_internal_token,
        settings.agent_runtime_timeout_seconds,
    )
    with suppress(Exception):
        await runtime.delete_session(
            tenant_id=user.tenant_id,
            project_id=project_id,
            session_id=chat_session.id,
        )

    for storage_path, _attachment in storage_items:
        with suppress(Exception):
            await delete_media_file(storage_path)


@router.get("/sessions/{session_id}", response_model=AgentChatSessionDetail)
async def get_session_detail(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatSessionDetail:
    await project_for_user(db, project_id, user)
    chat_session = await session_for_user(db, project_id, session_id, user)
    messages = list(
        (
            await db.scalars(
                select(AgentChatMessage)
                .where(AgentChatMessage.session_id == chat_session.id)
                .order_by(AgentChatMessage.created_at, AgentChatMessage.id)
            )
        ).all()
    )
    return AgentChatSessionDetail(
        session=chat_session,
        messages=messages,
        active_task=await active_chat_task(
            db,
            user=user,
            project_id=project_id,
            chat_session_id=chat_session.id,
        ),
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=AgentChatRunQueuedPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    project_id: str,
    session_id: str,
    payload: AgentChatMessageCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatRunQueuedPublic:
    project = await project_for_user(db, project_id, user)
    await serialize_project_task_submissions(db, project_id)
    chat_session = await session_for_user(db, project_id, session_id, user)
    scene = await chat_session_scene(db, chat_session)
    chapter: Chapter | None = None
    if scene == "director":
        if not payload.chapter_id:
            raise HTTPException(status_code=422, detail="导演台对话必须绑定当前章节")
        chapter = await db.get(Chapter, payload.chapter_id)
        if (
            chapter is None
            or chapter.project_id != project_id
            or chapter.tenant_id != user.tenant_id
            or chapter.user_id != user.id
        ):
            raise HTTPException(status_code=404, detail="当前导演台章节不存在")
    agent = await resolve_scene_agent(db, tenant_id=user.tenant_id, scene=scene)
    if chat_session.agent_profile_id != agent.id:
        chat_session.agent_profile_id = agent.id
        chat_session.runtime_manifest = {**(chat_session.runtime_manifest or {}), "scene": scene}
    pending = await active_chat_task(
        db,
        user=user,
        project_id=project_id,
        chat_session_id=chat_session.id,
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="当前会话已有 Agent 任务正在处理")
    attachments = await message_attachments(
        db,
        user=user,
        project_id=project_id,
        attachment_ids=payload.attachment_ids,
    )

    now = datetime.now(UTC)
    attachment_manifest = [attachment_public(item).model_dump(mode="json") for item in attachments]
    user_message = AgentChatMessage(
        id=new_id(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        session_id=chat_session.id,
        role=AgentMessageRole.USER,
        content=payload.content,
        runtime_manifest={"attachments": attachment_manifest} if attachment_manifest else None,
    )
    db.add(user_message)
    for attachment in attachments:
        attachment.file_metadata = {
            **attachment.file_metadata,
            "agent_chat_message_id": user_message.id,
            "agent_chat_session_id": chat_session.id,
        }
    if scene == "director" and chapter is not None and requested_storyboard_platform_action(payload.content):
        from app.services.director_orchestration import start_storyboard_workflow_for_chapter

        try:
            workflow = await start_storyboard_workflow_for_chapter(
                db,
                chapter=chapter,
                user=user,
                chat_session_id=chat_session.id,
            )
        except ValueError as error:
            return await persist_platform_action_failure(
                db,
                user=user,
                project=project,
                chat_session=chat_session,
                user_message=user_message,
                action="storyboard_workflow",
                title="导演分镜工作流",
                message=str(error),
                now=now,
            )
        except RuntimeError as error:
            return await persist_platform_action_failure(
                db,
                user=user,
                project=project,
                chat_session=chat_session,
                user_message=user_message,
                action="storyboard_workflow",
                title="导演分镜工作流",
                message=str(error),
                now=now,
            )
        if not workflow.current_task_id:
            return await persist_platform_action_failure(
                db,
                user=user,
                project=project,
                chat_session=chat_session,
                user_message=user_message,
                action="storyboard_workflow",
                title="导演分镜工作流",
                message="导演分镜工作流未能创建任务",
                now=now,
            )
        primary_task = await db.get(AITask, workflow.current_task_id)
        if primary_task is None:
            return await persist_platform_action_failure(
                db,
                user=user,
                project=project,
                chat_session=chat_session,
                user_message=user_message,
                action="storyboard_workflow",
                title="导演分镜工作流",
                message="导演分镜工作流任务不存在",
                now=now,
            )
        primary_event = await db.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == primary_task.id)
            .order_by(TaskEvent.created_at.desc())
            .limit(1)
        )
        user_message.run_id = primary_task.id
        outcome = {
            "operation": "queue_storyboard_workflow",
            "file_id": None,
            "name": "导演分镜工作流",
            "status": "applied",
            "reason": None,
            "resource_type": "director_workflow",
            "resource_id": workflow.id,
            "workflow_id": workflow.id,
            "task_id": primary_task.id,
            "chapter_id": chapter.id,
            "stage": workflow.stage.value,
        }
        runtime_manifest = {
            "runtime_type": "platform_action",
            "contract_version": "v1",
            "agent_chat_session_id": chat_session.id,
            "project_file_changes": [outcome],
            "domain_changes": [outcome],
            "platform_action": "storyboard_workflow",
        }
        assistant_message = AgentChatMessage(
            tenant_id=user.tenant_id,
            user_id=user.id,
            session_id=chat_session.id,
            role=AgentMessageRole.ASSISTANT,
            content=(
                "已启动导演分镜工作流。平台会先检查资产提示词和图片是否齐备，"
                "缺失时自动派发对应子任务；资产验证通过后再生成分镜并进入内部审核。"
                "你可以在导演流程卡片和通知中心查看每个子任务的状态。"
            ),
            run_id=primary_task.id,
            finish_reason="platform_action",
            runtime_events=[],
            runtime_manifest=runtime_manifest,
        )
        db.add(assistant_message)
        if chat_session.title == "新对话":
            chat_session.title = payload.content[:56] or "分镜工作流"
        chat_session.runtime_manifest = runtime_manifest
        chat_session.last_message_at = now
        await db.commit()
        await db.refresh(chat_session)
        await db.refresh(user_message)
        await db.refresh(primary_task)
        if primary_event is not None:
            await db.refresh(primary_event)
        task_public = TaskPublic.model_validate(primary_task).model_copy(
            update={
                "progress": primary_event.progress if primary_event else 0,
                "latest_message": primary_event.message if primary_event else workflow.last_message,
                "latest_event_at": primary_event.created_at if primary_event else None,
            }
        )
        return AgentChatRunQueuedPublic(
            session=chat_session,
            user_message=user_message,
            task=task_public,
        )

    platform_action = requested_asset_platform_action(payload.content) if not attachments else None
    if platform_action is not None:
        dispatches: list[tuple[AITask, TaskEvent]] = []
        target, asset_ids, asset_names = await inferred_asset_action_target(
            db,
            user=user,
            project=project,
            chapter=chapter,
            content=payload.content,
        )
        if platform_action == "prompt":
            command = json.dumps(
                {
                    "operation": "generate_asset_prompts",
                    "target": target,
                    "asset_ids": asset_ids,
                    "asset_names": asset_names,
                    "only_missing_prompt": True,
                    "auto_queue_images_after_prompt": False,
                },
                ensure_ascii=False,
            )
            outcome = await apply_asset_prompt_command(
                db,
                user,
                project,
                chapter,
                command,
                dispatches=dispatches,
            )
        else:
            command = json.dumps(
                {
                    "operation": "generate_asset_images",
                    "target": target,
                    "asset_ids": asset_ids,
                    "asset_names": asset_names,
                    "only_missing_image": True,
                    "auto_generate_missing_prompts": True,
                },
                ensure_ascii=False,
            )
            outcome = await apply_asset_image_command(
                db,
                user,
                project,
                chapter,
                command,
                dispatches=dispatches,
            )
        if outcome.get("status") != "applied" or not dispatches:
            raise HTTPException(status_code=409, detail=outcome.get("reason") or "资产任务未能创建")
        primary_task, primary_event = dispatches[0]
        user_message.run_id = primary_task.id
        prompt_count = int(outcome.get("prompt_asset_count") or 0)
        image_count = int(outcome.get("asset_count") or 0)
        if platform_action == "prompt":
            assistant_content = (
                f"已通过资产库【生成提示词】通道为 {outcome.get('asset_count') or 0} 个资产创建任务。"
                "你可以在通知中心查看排队、进行中、完成或失败状态。"
            )
        elif prompt_count and image_count:
            assistant_content = (
                f"已通过资产库通道安排：{prompt_count} 个资产先生成提示词，"
                f"{image_count} 个提示词已就绪的资产已进入【生图】队列。"
                "提示词任务完成后会自动继续排同批资产的生图任务。"
            )
        elif prompt_count:
            assistant_content = (
                f"已通过资产库【生成提示词】通道为 {prompt_count} 个资产创建任务，"
                "提示词完成后会自动进入【生图】队列。你可以在通知中心跟踪进度。"
            )
        else:
            assistant_content = (
                f"已通过资产库【生图】通道为 {image_count} 个资产创建任务。"
                "你可以在通知中心查看排队、进行中、完成或失败状态。"
            )
        runtime_manifest = {
            "runtime_type": "platform_action",
            "contract_version": "v1",
            "agent_chat_session_id": chat_session.id,
            "project_file_changes": [outcome],
            "domain_changes": [outcome],
            "platform_action": platform_action,
        }
        assistant_message = AgentChatMessage(
            tenant_id=user.tenant_id,
            user_id=user.id,
            session_id=chat_session.id,
            role=AgentMessageRole.ASSISTANT,
            content=assistant_content,
            run_id=primary_task.id,
            finish_reason="platform_action",
            runtime_events=[],
            runtime_manifest=runtime_manifest,
        )
        db.add(assistant_message)
        if chat_session.title == "新对话":
            chat_session.title = payload.content[:56] or "资产任务"
        chat_session.runtime_manifest = runtime_manifest
        chat_session.last_message_at = now
        await db.flush()
        await db.commit()
        await db.refresh(chat_session)
        await db.refresh(user_message)
        await db.refresh(primary_task)
        await db.refresh(primary_event)
        for queued_task, queued_event in dispatches:
            await enqueue_task(queued_task.id)
            await publish_task_event(queued_task, queued_event)
        task_public = TaskPublic.model_validate(primary_task).model_copy(
            update={
                "progress": primary_event.progress,
                "latest_message": primary_event.message,
                "latest_event_at": primary_event.created_at,
            }
        )
        return AgentChatRunQueuedPublic(
            session=chat_session,
            user_message=user_message,
            task=task_public,
        )

    model, _provider, _api_key = await resolve_text_model(db, agent, user.tenant_id)
    pricing = await resolve_task_pricing(
        db,
        tenant_id=user.tenant_id,
        task_type="agent_chat_run",
    )
    task, event = await create_queued_task(
        db,
        user=user,
        project_id=project_id,
        task_type="agent_chat_run",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "agent_chat_session_id": chat_session.id,
            "user_message_id": user_message.id,
            "agent_profile_id": agent.id,
            "agent_version": agent.version,
            "prompt_hash": hashlib.sha256(payload.content.encode("utf-8")).hexdigest(),
            "attachment_ids": [item.id for item in attachments],
            "chapter_id": chapter.id if chapter else None,
            "chapter_source_hash": (
                hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
                if chapter
                else None
            ),
            "pricing": pricing.as_payload(),
        },
        message="Agent 创作回复",
    )
    user_message.run_id = task.id
    if chat_session.title == "新对话":
        chat_session.title = payload.content[:56] or "图片对话"
    chat_session.last_message_at = now
    await db.commit()
    await db.refresh(chat_session)
    await db.refresh(user_message)
    await db.refresh(task)
    await db.refresh(event)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    task_public = TaskPublic.model_validate(task).model_copy(
        update={
            "progress": event.progress,
            "latest_message": event.message,
            "latest_event_at": event.created_at,
        }
    )
    return AgentChatRunQueuedPublic(
        session=chat_session,
        user_message=user_message,
        task=task_public,
    )


async def personal_session_for_user(
    db: AsyncSession,
    session_id: str,
    user: User,
) -> AgentChatSession:
    chat_session = await db.get(AgentChatSession, session_id)
    if (
        chat_session is None
        or chat_session.tenant_id != user.tenant_id
        or chat_session.user_id != user.id
        or chat_session.project_id is not None
        or (chat_session.runtime_manifest or {}).get("scope") != "personal"
    ):
        raise HTTPException(status_code=404, detail="个人 Agent 会话不存在")
    return chat_session


async def personal_message_attachments(
    db: AsyncSession,
    *,
    user: User,
    attachment_ids: list[str],
) -> list[PersonalAgentAttachment]:
    if not attachment_ids:
        return []
    rows = list(
        (
            await db.scalars(
                select(PersonalAgentAttachment).where(
                    PersonalAgentAttachment.id.in_(attachment_ids),
                    PersonalAgentAttachment.tenant_id == user.tenant_id,
                    PersonalAgentAttachment.user_id == user.id,
                )
            )
        ).all()
    )
    by_id = {item.id: item for item in rows}
    attachments: list[PersonalAgentAttachment] = []
    for attachment_id in attachment_ids:
        item = by_id.get(attachment_id)
        if (
            item is None
            or item.message_id
            or not item.storage_path
            or not item.mime_type.startswith("image/")
        ):
            raise HTTPException(status_code=422, detail="图片附件不存在、已被使用或不属于当前用户")
        attachments.append(item)
    return attachments


async def selected_personal_skills(
    db: AsyncSession,
    *,
    user: User,
    skill_ids: list[str],
) -> list[UserSkill]:
    if not skill_ids:
        return []
    rows = list(
        (
            await db.scalars(
                select(UserSkill).where(
                    UserSkill.id.in_(skill_ids),
                    UserSkill.tenant_id == user.tenant_id,
                    UserSkill.user_id == user.id,
                    UserSkill.enabled.is_(True),
                )
            )
        ).all()
    )
    by_id = {item.id: item for item in rows}
    if any(skill_id not in by_id for skill_id in skill_ids):
        raise HTTPException(status_code=422, detail="所选 Skill 不存在、已禁用或不属于当前用户")
    return [by_id[skill_id] for skill_id in skill_ids]


async def default_personal_media_model(
    db: AsyncSession,
    *,
    tenant_id: str,
    model_type: ModelType,
    resolution: str | None = None,
    model_id: str | None = None,
) -> tuple[AIModel, Provider]:
    if model_type == ModelType.IMAGE:
        normalized_resolution = normalize_image_resolution(resolution or "1K")
        if normalized_resolution is None:
            raise HTTPException(status_code=422, detail="图片分辨率仅支持 1K、2K 或 4K")
        model = await db.get(AIModel, model_id) if model_id else None
        if model is None:
            model = await resolve_image_model(db, tenant_id=tenant_id, resolution=normalized_resolution)
    else:
        model = await db.get(AIModel, model_id) if model_id else None
        if model is None:
            model = await db.scalar(
            select(AIModel).where(
                AIModel.tenant_id == tenant_id,
                AIModel.model_type == model_type,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
            )
    if model is None:
        label = "图片" if model_type == ModelType.IMAGE else "视频"
        configuration = (
            f"{normalized_resolution} 图片模型路由"
            if model_type == ModelType.IMAGE
            else "默认视频模型"
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"管理员尚未配置可用的{configuration}，请前往管理后台 → 模型平台 → "
                f"全局默认模型完成配置"
            ),
        )
    if model.tenant_id != tenant_id or model.model_type != model_type or not model.enabled:
        raise HTTPException(status_code=422, detail="所选模型不可用或与当前功能类型不匹配")
    provider = await db.get(Provider, model.provider_id)
    if provider is None or provider.tenant_id != tenant_id or not provider.enabled:
        label = "图片" if model_type == ModelType.IMAGE else "视频"
        raise HTTPException(
            status_code=422,
            detail=(
                f"{label}模型所属平台不可用，请管理员前往管理后台 → 模型平台检查平台状态"
            ),
        )
    return model, provider


@personal_router.get("/options", response_model=AgentChatOptions)
async def personal_chat_options(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatOptions:
    agent = await resolve_scene_agent(db, tenant_id=user.tenant_id, scene="workspace")
    models = list(
        (
            await db.scalars(
                select(AIModel)
                .where(AIModel.tenant_id == user.tenant_id, AIModel.enabled.is_(True))
                .order_by(AIModel.model_type, AIModel.is_default.desc(), AIModel.name)
            )
        ).all()
    )
    return AgentChatOptions(
        agents=[AgentOptionPublic.model_validate(agent)],
        skills=[],
        text_models=[
            ModelPublic.model_validate(item) for item in models if item.model_type == ModelType.TEXT
        ],
        image_models=[
            ModelPublic.model_validate(item) for item in models if item.model_type == ModelType.IMAGE
        ],
        video_models=[
            ModelPublic.model_validate(item) for item in models if item.model_type == ModelType.VIDEO
        ],
        tts_models=[
            ModelPublic.model_validate(item) for item in models if item.model_type == ModelType.TTS
        ],
    )


@personal_router.post(
    "/attachments",
    response_model=AgentChatAttachmentPublic,
    status_code=status.HTTP_201_CREATED,
)
async def upload_personal_attachment(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatAttachmentPublic:
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_COVER_TYPES:
        await file.close()
        raise HTTPException(status_code=422, detail="仅支持 JPG、PNG 或 WebP 图片")
    data = await file.read(MAX_COVER_BYTES + 1)
    original_name = Path(file.filename or "图片").name
    await file.close()
    if len(data) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="单张图片不能超过 8MB")

    stored_path: Path | None = None
    storage_key: str | None = None
    try:
        stored_path = await run_in_threadpool(
            save_agent_chat_image,
            data,
            uploads_root=settings.uploads_root,
            tenant_id=user.tenant_id,
            project_id=f"personal-{user.id}",
        )
        storage_key, media_url = await persist_media_file(stored_path, "image/webp")
        attachment = PersonalAgentAttachment(
            tenant_id=user.tenant_id,
            user_id=user.id,
            name=f"{Path(original_name).stem[:220] or '图片'}.webp",
            mime_type="image/webp",
            size_bytes=stored_path.stat().st_size,
            storage_path=storage_key,
            media_url=media_url,
        )
        db.add(attachment)
        await db.commit()
        await db.refresh(attachment)
        return personal_attachment_public(attachment)
    except InvalidCoverImage as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        await db.rollback()
        if storage_key:
            with suppress(Exception):
                await delete_media_file(storage_key, stored_path)
        elif stored_path:
            with suppress(OSError):
                stored_path.unlink(missing_ok=True)
        raise


@personal_router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_personal_attachment(
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    attachment = await db.get(PersonalAgentAttachment, attachment_id)
    if (
        attachment is None
        or attachment.tenant_id != user.tenant_id
        or attachment.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="图片附件不存在")
    if attachment.message_id:
        raise HTTPException(status_code=409, detail="已发送的图片附件不能删除")
    storage_path = attachment.storage_path
    await db.delete(attachment)
    await db.commit()
    with suppress(Exception):
        await delete_media_file(storage_path)


@personal_router.get("/sessions", response_model=list[AgentChatSessionPublic])
async def list_personal_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[AgentChatSession]:
    return list(
        (
            await db.scalars(
                select(AgentChatSession)
                .where(
                    AgentChatSession.tenant_id == user.tenant_id,
                    AgentChatSession.user_id == user.id,
                    AgentChatSession.project_id.is_(None),
                )
                .order_by(AgentChatSession.last_message_at.desc())
                .limit(30)
            )
        ).all()
    )


@personal_router.post(
    "/sessions",
    response_model=AgentChatSessionPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_personal_session(
    _payload: AgentChatSessionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatSession:
    agent = await resolve_scene_agent(db, tenant_id=user.tenant_id, scene="workspace")
    chat_session = AgentChatSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=None,
        agent_profile_id=agent.id,
        runtime_manifest={"scene": "workspace", "scope": "personal"},
    )
    db.add(chat_session)
    await db.commit()
    await db.refresh(chat_session)
    return chat_session


@personal_router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_personal_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    chat_session = await personal_session_for_user(db, session_id, user)
    pending = await active_chat_task(
        db,
        user=user,
        project_id=None,
        chat_session_id=chat_session.id,
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="当前对话仍在生成，请先停止后再删除")
    attachments = list(
        (
            await db.scalars(
                select(PersonalAgentAttachment).where(
                    PersonalAgentAttachment.session_id == chat_session.id
                )
            )
        ).all()
    )
    storage_paths = [item.storage_path for item in attachments]
    await db.delete(chat_session)
    await db.commit()

    runtime = AgentRuntimeClient(
        settings.agent_runtime_url,
        settings.agent_runtime_internal_token,
        settings.agent_runtime_timeout_seconds,
    )
    with suppress(Exception):
        await runtime.delete_session(
            tenant_id=user.tenant_id,
            project_id=f"personal-{user.id}",
            session_id=chat_session.id,
        )
    for storage_path in storage_paths:
        with suppress(Exception):
            await delete_media_file(storage_path)


@personal_router.get("/sessions/{session_id}", response_model=AgentChatSessionDetail)
async def get_personal_session_detail(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatSessionDetail:
    chat_session = await personal_session_for_user(db, session_id, user)
    messages = list(
        (
            await db.scalars(
                select(AgentChatMessage)
                .where(AgentChatMessage.session_id == chat_session.id)
                .order_by(AgentChatMessage.created_at, AgentChatMessage.id)
            )
        ).all()
    )
    return AgentChatSessionDetail(
        session=chat_session,
        messages=messages,
        active_task=await active_chat_task(
            db,
            user=user,
            project_id=None,
            chat_session_id=chat_session.id,
        ),
    )


@personal_router.post(
    "/sessions/{session_id}/messages",
    response_model=AgentChatRunQueuedPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_personal_message(
    session_id: str,
    payload: AgentChatMessageCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AgentChatRunQueuedPublic:
    chat_session = await personal_session_for_user(db, session_id, user)
    agent = await resolve_scene_agent(db, tenant_id=user.tenant_id, scene="workspace")
    if chat_session.agent_profile_id != agent.id:
        chat_session.agent_profile_id = agent.id
    pending = await active_chat_task(
        db,
        user=user,
        project_id=None,
        chat_session_id=chat_session.id,
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="当前会话已有 Agent 任务正在处理")
    attachments = await personal_message_attachments(
        db,
        user=user,
        attachment_ids=payload.attachment_ids,
    )
    selected_skills = await selected_personal_skills(
        db,
        user=user,
        skill_ids=payload.skill_ids,
    )
    now = datetime.now(UTC)
    attachment_manifest = [personal_attachment_public(item).model_dump(mode="json") for item in attachments]
    selection_manifest = [
        {"id": skill.id, "name": skill.name, "version": skill.version}
        for skill in selected_skills
    ]
    message_manifest: dict[str, Any] = {
        "mode": payload.mode,
        "selected_skills": selection_manifest,
        "media_options": payload.media_options.model_dump(exclude_none=True),
    }
    if attachment_manifest:
        message_manifest["attachments"] = attachment_manifest
    user_message = AgentChatMessage(
        id=new_id(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        session_id=chat_session.id,
        role=AgentMessageRole.USER,
        content=payload.content,
        runtime_manifest=message_manifest,
    )
    db.add(user_message)
    for attachment in attachments:
        attachment.session_id = chat_session.id
        attachment.message_id = user_message.id

    text_model, _text_provider, _api_key = await resolve_text_model(
        db, agent, user.tenant_id, payload.text_model_id
    )
    media_model: AIModel | None = None
    bill_task_type = "agent_chat_run"
    queue_message = "个人 Agent 回复"
    if payload.mode == "image":
        media_model, _media_provider = await default_personal_media_model(
            db,
            tenant_id=user.tenant_id,
            model_type=ModelType.IMAGE,
            resolution=payload.media_options.resolution or "1K",
            model_id=payload.media_model_id,
        )
        bill_task_type = "asset_image_generation"
        queue_message = "个人图片生成"
    elif payload.mode == "video":
        media_model, _media_provider = await default_personal_media_model(
            db,
            tenant_id=user.tenant_id,
            model_type=ModelType.VIDEO,
            model_id=payload.media_model_id,
        )
        bill_task_type = "shot_video_generation"
        queue_message = "个人视频生成"
    pricing = await resolve_task_pricing(
        db,
        tenant_id=user.tenant_id,
        task_type=bill_task_type,
    )
    task, event = await create_queued_task(
        db,
        user=user,
        project_id=None,
        task_type="agent_chat_run",
        model_id=(media_model or text_model).id,
        cost=pricing.total_cost,
        request_payload={
            "scope": "personal",
            "mode": payload.mode,
            "agent_chat_session_id": chat_session.id,
            "user_message_id": user_message.id,
            "agent_profile_id": agent.id,
            "agent_version": agent.version,
            "text_model_id": text_model.id,
            "media_model_id": media_model.id if media_model else None,
            "selected_skill_ids": [skill.id for skill in selected_skills],
            "selected_skill_versions": {
                skill.id: skill.version for skill in selected_skills
            },
            "media_options": payload.media_options.model_dump(exclude_none=True),
            "prompt_hash": hashlib.sha256(payload.content.encode("utf-8")).hexdigest(),
            "attachment_ids": [item.id for item in attachments],
            "pricing": pricing.as_payload(),
        },
        message=queue_message,
    )
    user_message.run_id = task.id
    if chat_session.title == "新对话":
        fallback_title = {
            "image": "图片创作",
            "video": "视频创作",
            "skill": "Skill 创作",
        }.get(payload.mode, "图片对话")
        chat_session.title = payload.content[:56] or fallback_title
    chat_session.last_message_at = now
    chat_session.runtime_manifest = {
        **(chat_session.runtime_manifest or {}),
        "scene": "workspace",
        "scope": "personal",
        "last_mode": payload.mode,
    }
    await db.commit()
    await db.refresh(chat_session)
    await db.refresh(user_message)
    await db.refresh(task)
    await db.refresh(event)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return AgentChatRunQueuedPublic(
        session=chat_session,
        user_message=user_message,
        task=TaskPublic.model_validate(task).model_copy(
            update={
                "progress": event.progress,
                "latest_message": event.message,
                "latest_event_at": event.created_at,
            }
        ),
    )
