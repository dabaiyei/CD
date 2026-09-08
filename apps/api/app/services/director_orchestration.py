from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentChatSession,
    AgentKind,
    AgentProfile,
    AIModel,
    AITask,
    Asset,
    AssetExtraction,
    AssetExtractionItem,
    AssetStatus,
    Chapter,
    ChapterAnalysis,
    ChapterStatus,
    DirectorChildRun,
    DirectorChildStatus,
    DirectorDecisionRequest,
    DirectorWorkflowRun,
    DirectorWorkflowStage,
    DirectorWorkflowStatus,
    ModelType,
    Project,
    ProjectFile,
    ProjectFileKind,
    Provider,
    ScriptReview,
    ScriptReviewDecision,
    ScriptVersion,
    StoryboardShot,
    StoryboardVersion,
    TaskEvent,
    TaskStatus,
    User,
    VideoClip,
    VideoClipStatus,
)
from app.services.billing import refund_task_cost, resolve_task_pricing
from app.services.composition import invalidate_compositions
from app.services.image_model_routing import resolve_image_model
from app.services.object_storage import materialize_media_file, object_key_from_media_url
from app.services.provider_adapters import (
    compatible_video_resolution,
    default_video_audio_enabled,
)
from app.services.task_events import publish_task_event, record_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import create_queued_task

REVIEW_OPTIONS = [
    {"value": "partial_repair", "label": "修复指定问题", "description": "保留可用内容，只修复审核指出的部分"},
    {"value": "full_rewrite", "label": "整版重做", "description": "保留原始目标，重新生成完整版本"},
    {"value": "continue_anyway", "label": "忽略问题继续", "description": "记录风险并进入下一生产阶段"},
    {
        "value": "provide_feedback",
        "label": "补充意见后修复",
        "description": "加入你的具体意见，再执行局部修复",
    },
]

ACTIVE_WORKFLOW_STATUSES = {
    DirectorWorkflowStatus.RUNNING,
    DirectorWorkflowStatus.WAITING_USER,
}

BUSY_STORYBOARD_STAGES = {
    DirectorWorkflowStage.SCRIPT_ADAPTING,
    DirectorWorkflowStage.SCRIPT_REVIEWING,
    DirectorWorkflowStage.SCRIPT_REPAIRING,
    DirectorWorkflowStage.ASSET_EXTRACTING,
    DirectorWorkflowStage.ASSET_PREPARING,
    DirectorWorkflowStage.STORYBOARD_GENERATING,
    DirectorWorkflowStage.STORYBOARD_REVIEWING,
    DirectorWorkflowStage.STORYBOARD_REPAIRING,
}

CHILD_TITLES = {
    "script_adaptation": "改编 AI 视频剧本",
    "script_review": "审核剧本可拍摄性",
    "script_repair": "修复剧本审核问题",
    "asset_extraction": "提取人物、场景与道具",
    "asset_prompt": "补全资产生图提示词",
    "asset_image": "生成资产定稿图",
    "storyboard_generation": "制作导演分镜表",
    "storyboard_review": "审核分镜连续性",
    "storyboard_repair": "修复分镜审核问题",
    "video_prompt": "生成镜头视频提示词",
    "video_generation": "生成镜头视频",
}


async def active_automatic_workflow(
    session: AsyncSession,
    *,
    chapter_id: str,
    user_id: str | None = None,
) -> DirectorWorkflowRun | None:
    query = select(DirectorWorkflowRun).where(
        DirectorWorkflowRun.chapter_id == chapter_id,
        DirectorWorkflowRun.automation_mode.is_(True),
        DirectorWorkflowRun.status.in_(ACTIVE_WORKFLOW_STATUSES),
    )
    if user_id:
        query = query.where(DirectorWorkflowRun.user_id == user_id)
    return await session.scalar(query.order_by(DirectorWorkflowRun.created_at.desc()).limit(1))


async def ensure_chapter_not_automating(
    session: AsyncSession,
    *,
    chapter_id: str,
    user_id: str | None = None,
) -> None:
    if await active_automatic_workflow(session, chapter_id=chapter_id, user_id=user_id):
        raise ValueError("当前章节正在由 AI 全自动制作，完成或手动停止前不能修改章节内容")


async def ensure_asset_not_automating(
    session: AsyncSession,
    *,
    asset_id: str,
    user_id: str,
) -> None:
    workflow_id = await session.scalar(
        select(DirectorWorkflowRun.id)
        .join(
            AssetExtractionItem,
            AssetExtractionItem.extraction_id == DirectorWorkflowRun.asset_extraction_id,
        )
        .where(
            AssetExtractionItem.asset_id == asset_id,
            DirectorWorkflowRun.user_id == user_id,
            DirectorWorkflowRun.automation_mode.is_(True),
            DirectorWorkflowRun.status.in_(ACTIVE_WORKFLOW_STATUSES),
        )
        .limit(1)
    )
    if workflow_id:
        raise ValueError("该资产正在被 AI 全自动流程使用，完成或手动停止前不能修改")


async def workflow_for_user(
    session: AsyncSession,
    *,
    workflow_id: str,
    project_id: str,
    user: User,
) -> DirectorWorkflowRun | None:
    return await session.scalar(
        select(DirectorWorkflowRun).where(
            DirectorWorkflowRun.id == workflow_id,
            DirectorWorkflowRun.project_id == project_id,
            DirectorWorkflowRun.tenant_id == user.tenant_id,
            DirectorWorkflowRun.user_id == user.id,
        )
    )


async def workflow_detail_rows(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> tuple[list[DirectorChildRun], DirectorDecisionRequest | None]:
    children = list(
        (
            await session.scalars(
                select(DirectorChildRun)
                .where(
                    DirectorChildRun.workflow_id == workflow.id,
                    DirectorChildRun.user_id == workflow.user_id,
                )
                .order_by(DirectorChildRun.created_at)
            )
        ).all()
    )
    decision = await session.scalar(
        select(DirectorDecisionRequest)
        .where(
            DirectorDecisionRequest.workflow_id == workflow.id,
            DirectorDecisionRequest.user_id == workflow.user_id,
            DirectorDecisionRequest.resolved.is_(False),
        )
        .order_by(DirectorDecisionRequest.created_at.desc())
        .limit(1)
    )
    return children, decision


async def _agent_and_model(
    session: AsyncSession,
    tenant_id: str,
    *,
    kind: AgentKind = AgentKind.SCREENPLAY,
    project_id: str | None = None,
) -> tuple[AgentProfile, AIModel]:
    agent = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.kind == kind,
            AgentProfile.enabled.is_(True),
        )
    )
    project = await session.get(Project, project_id) if project_id else None
    selected_model_id = project.text_model_id if project and project.creation_mode == "ai" else None
    if agent is None or not (selected_model_id or agent.text_model_id):
        raise RuntimeError("管理员尚未配置可用的导演 Agent 与文本模型")
    model = await session.get(AIModel, selected_model_id or agent.text_model_id)
    if model is None or model.model_type != ModelType.TEXT or not model.enabled:
        raise RuntimeError("导演 Agent 绑定的文本模型不可用")
    return agent, model


async def _queue_child(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    *,
    kind: str,
    task_type: str,
    request_payload: dict[str, Any],
    message: str,
    model_id: str | None = None,
    agent_kind: AgentKind = AgentKind.SCREENPLAY,
    bill_task_type: str | None = None,
    quantity: int = 1,
    parent: DirectorChildRun | None = None,
    attempt: int = 1,
) -> tuple[AITask, TaskEvent, DirectorChildRun]:
    user = await session.get(User, workflow.user_id)
    if user is None:
        raise RuntimeError("导演流程用户已不存在")
    agent: AgentProfile | None = None
    if model_id is None:
        agent, model = await _agent_and_model(
            session, workflow.tenant_id, kind=agent_kind, project_id=workflow.project_id,
        )
        model_id = model.id
    pricing_payload: dict[str, Any] = {"unit_cost": "0", "quantity": quantity, "total_cost": "0"}
    cost = Decimal("0")
    if bill_task_type:
        pricing = await resolve_task_pricing(
            session,
            tenant_id=workflow.tenant_id,
            task_type=bill_task_type,
            quantity=quantity,
        )
        pricing_payload = pricing.as_payload()
        cost = pricing.total_cost
    payload = {
        **request_payload,
        "workflow_id": workflow.id,
        "pricing": pricing_payload,
    }
    if agent is not None:
        payload["agent_profile_id"] = agent.id
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=workflow.project_id,
        task_type=task_type,
        model_id=model_id,
        cost=cost,
        request_payload=payload,
        message=message,
    )
    child = DirectorChildRun(
        tenant_id=workflow.tenant_id,
        user_id=workflow.user_id,
        project_id=workflow.project_id,
        workflow_id=workflow.id,
        parent_child_run_id=parent.id if parent else None,
        task_id=task.id,
        kind=kind,
        title=CHILD_TITLES[kind],
        status=DirectorChildStatus.QUEUED,
        attempt=attempt,
        max_attempts=5 if workflow.automation_mode else 3,
        input_refs={key: value for key, value in request_payload.items() if key.endswith("_id")},
    )
    session.add(child)
    await session.flush()
    task.request_payload = {**task.request_payload, "child_run_id": child.id}
    workflow.current_task_id = task.id
    workflow.status = DirectorWorkflowStatus.RUNNING
    workflow.last_error = None
    workflow.last_message = f"{message}已交给子智能体"
    return task, event, child


async def _commit_and_dispatch(
    session: AsyncSession,
    queued: list[tuple[AITask, TaskEvent]],
) -> None:
    await session.commit()
    for task, event in queued:
        await enqueue_task(task.id)
        await publish_task_event(task, event)


async def start_script_workflow(
    session: AsyncSession,
    *,
    chapter: Chapter,
    user: User,
    instruction: str,
    chat_session_id: str | None,
    automation_mode: bool = False,
) -> DirectorWorkflowRun:
    if not chapter.original_content.strip():
        raise ValueError("当前章节没有可供改编的原始文章内容")
    existing = await session.scalar(
        select(DirectorWorkflowRun).where(
            DirectorWorkflowRun.chapter_id == chapter.id,
            DirectorWorkflowRun.status.in_(ACTIVE_WORKFLOW_STATUSES),
        )
    )
    if existing is not None:
        raise ValueError("当前章节已有进行中的导演流程")
    analysis = await session.scalar(
        select(ChapterAnalysis)
        .where(ChapterAnalysis.chapter_id == chapter.id)
        .order_by(ChapterAnalysis.version.desc())
        .limit(1)
    )
    workflow = DirectorWorkflowRun(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=chapter.project_id,
        chapter_id=chapter.id,
        chat_session_id=chat_session_id,
        stage=DirectorWorkflowStage.SCRIPT_ADAPTING,
        status=DirectorWorkflowStatus.RUNNING,
        automation_mode=automation_mode,
        stop_requested=False,
        context_snapshot={
            "source_hash": hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest(),
            "analysis_id": analysis.id if analysis else None,
            "instruction": instruction,
            "script_version_count": 0,
            "storyboard_version_count": 0,
            "automation_mode": automation_mode,
        },
        last_message="正在读取章节、项目记忆和导演 Skills",
    )
    session.add(workflow)
    await session.flush()
    task, event, _child = await _queue_child(
        session,
        workflow,
        kind="script_adaptation",
        task_type="chapter_script_generation",
        request_payload={
            "chapter_id": chapter.id,
            "source_file_id": chapter.source_file_id,
            "source_hash": workflow.context_snapshot["source_hash"],
            "analysis_id": analysis.id if analysis else None,
            "base_script_version_id": None,
            "director_instruction": instruction,
        },
        message="AI 视频剧本改编",
        bill_task_type="chapter_script_generation",
    )
    chapter.status = ChapterStatus.SCRIPTING
    await _commit_and_dispatch(session, [(task, event)])
    await session.refresh(workflow)
    return workflow


async def start_automatic_workflow_from_progress(
    session: AsyncSession,
    *,
    chapter: Chapter,
    user: User,
    instruction: str,
    chat_session_id: str | None,
) -> DirectorWorkflowRun:
    """Start automatic production at the first incomplete persisted chapter stage."""
    if not chapter.original_content.strip():
        raise ValueError("当前章节没有可供改编的原始文章内容")
    existing = await session.scalar(
        select(DirectorWorkflowRun).where(
            DirectorWorkflowRun.chapter_id == chapter.id,
            DirectorWorkflowRun.status.in_(ACTIVE_WORKFLOW_STATUSES),
        )
    )
    if existing is not None:
        raise ValueError("当前章节已有进行中的导演流程")

    script = (
        await session.get(ScriptVersion, chapter.active_script_version_id)
        if chapter.active_script_version_id
        else None
    )
    if (
        script is None
        or script.chapter_id != chapter.id
        or script.tenant_id != user.tenant_id
        or script.user_id != user.id
        or not script.is_active
    ):
        return await start_script_workflow(
            session,
            chapter=chapter,
            user=user,
            instruction=instruction,
            chat_session_id=chat_session_id,
            automation_mode=True,
        )

    analysis = await session.scalar(
        select(ChapterAnalysis)
        .where(ChapterAnalysis.chapter_id == chapter.id)
        .order_by(ChapterAnalysis.version.desc())
        .limit(1)
    )
    workflow = DirectorWorkflowRun(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=chapter.project_id,
        chapter_id=chapter.id,
        chat_session_id=chat_session_id,
        stage=DirectorWorkflowStage.SCRIPT_REVIEWING,
        status=DirectorWorkflowStatus.RUNNING,
        automation_mode=True,
        stop_requested=False,
        script_version_id=script.id,
        context_snapshot={
            "source_hash": hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest(),
            "analysis_id": analysis.id if analysis else None,
            "instruction": instruction,
            "script_version_count": 0,
            "storyboard_version_count": 0,
            "automation_mode": True,
            "resumed_from_chapter_progress": True,
        },
        last_message=f"检测到生效剧本 v{script.version}，正在核验后续制作进度",
    )
    session.add(workflow)
    await session.flush()

    queued: list[tuple[AITask, TaskEvent]] = []
    if script.status != "approved":
        chapter.status = ChapterStatus.REVIEWING
        queued.append(await _queue_review(session, workflow, target="script", parent=None))
        await _commit_and_dispatch(session, queued)
        await session.refresh(workflow)
        return workflow

    extraction = await session.scalar(
        select(AssetExtraction)
        .where(
            AssetExtraction.tenant_id == user.tenant_id,
            AssetExtraction.user_id == user.id,
            AssetExtraction.project_id == chapter.project_id,
            AssetExtraction.chapter_id == chapter.id,
            AssetExtraction.script_version_id == script.id,
            AssetExtraction.is_active.is_(True),
        )
        .order_by(AssetExtraction.version.desc())
        .limit(1)
    )
    if extraction is None:
        chapter.status = ChapterStatus.ASSETS
        queued.append(await _queue_asset_extraction(session, workflow, None))
        await _commit_and_dispatch(session, queued)
        await session.refresh(workflow)
        return workflow

    workflow.asset_extraction_id = extraction.id
    assets = await _extraction_assets(session, workflow)
    if not assets:
        chapter.status = ChapterStatus.ASSETS
        queued.append(await _queue_asset_extraction(session, workflow, None))
        await _commit_and_dispatch(session, queued)
        await session.refresh(workflow)
        return workflow

    missing_assets = await _missing_ready_asset_names(assets)
    if missing_assets:
        chapter.status = ChapterStatus.ASSETS
        queued.extend(await _queue_asset_preparation(session, workflow, assets))
        await _commit_and_dispatch(session, queued)
        await session.refresh(workflow)
        return workflow

    storyboard = await session.scalar(
        select(StoryboardVersion)
        .where(
            StoryboardVersion.tenant_id == user.tenant_id,
            StoryboardVersion.user_id == user.id,
            StoryboardVersion.project_id == chapter.project_id,
            StoryboardVersion.chapter_id == chapter.id,
            StoryboardVersion.script_version_id == script.id,
            StoryboardVersion.is_active.is_(True),
        )
        .order_by(StoryboardVersion.version.desc())
        .limit(1)
    )
    shots = []
    if storyboard is not None:
        shots = list(
            (
                await session.scalars(
                    select(StoryboardShot)
                    .where(StoryboardShot.storyboard_version_id == storyboard.id)
                    .order_by(StoryboardShot.order_index)
                )
            ).all()
        )
    if storyboard is None or not shots:
        chapter.status = ChapterStatus.STORYBOARD
        queued.append(await _queue_storyboard(session, workflow))
        await _commit_and_dispatch(session, queued)
        await session.refresh(workflow)
        return workflow

    workflow.storyboard_version_id = storyboard.id
    missing_prompt_shots = [shot for shot in shots if not shot.video_prompt.strip()]
    if missing_prompt_shots:
        chapter.status = ChapterStatus.VIDEO
        queued.append(
            await _queue_video_prompts(
                session,
                workflow,
                None,
                shots=missing_prompt_shots,
            )
        )
        await _commit_and_dispatch(session, queued)
        await session.refresh(workflow)
        return workflow

    ready_video_shot_ids = set(
        (
            await session.scalars(
                select(VideoClip.shot_id).where(
                    VideoClip.storyboard_version_id == storyboard.id,
                    VideoClip.is_active.is_(True),
                    VideoClip.status == VideoClipStatus.READY,
                    VideoClip.media_url.is_not(None),
                )
            )
        ).all()
    )
    if all(shot.id in ready_video_shot_ids for shot in shots):
        chapter.status = ChapterStatus.COMPLETED
        workflow.stage = DirectorWorkflowStage.READY_FOR_VIDEO
        workflow.status = DirectorWorkflowStatus.COMPLETED
        workflow.current_task_id = None
        workflow.last_error = None
        workflow.last_message = f"检测到 {len(shots)} 个镜头视频均已完成，无需重复生成"
        await session.commit()
        await session.refresh(workflow)
        return workflow

    chapter.status = ChapterStatus.VIDEO
    queued.extend(await _queue_video_generation(session, workflow, None))
    if queued:
        await _commit_and_dispatch(session, queued)
    else:
        await session.commit()
    await session.refresh(workflow)
    return workflow


async def _activate_script(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    script: ScriptVersion,
) -> None:
    chapter = await session.get(Chapter, workflow.chapter_id)
    if chapter is None:
        raise RuntimeError("导演流程章节已不存在")
    await session.execute(
        update(ScriptVersion).where(ScriptVersion.chapter_id == chapter.id).values(is_active=False)
    )
    script.is_active = True
    chapter.active_script_version_id = script.id
    chapter.status = ChapterStatus.REVIEWING
    await invalidate_compositions(session, chapter_id=chapter.id, reason="导演 Agent 已生成新的生效剧本")
    workflow.script_version_id = script.id


async def _queue_review(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    *,
    target: str,
    parent: DirectorChildRun | None,
) -> tuple[AITask, TaskEvent]:
    if target == "script":
        workflow.stage = DirectorWorkflowStage.SCRIPT_REVIEWING
        kind = "script_review"
        refs = {"chapter_id": workflow.chapter_id, "script_version_id": workflow.script_version_id}
        task_type = "director_script_review"
        message = "剧本审核"
    else:
        workflow.stage = DirectorWorkflowStage.STORYBOARD_REVIEWING
        kind = "storyboard_review"
        refs = {
            "chapter_id": workflow.chapter_id,
            "storyboard_version_id": workflow.storyboard_version_id,
        }
        task_type = "director_storyboard_review"
        message = "分镜审核"
    task, event, _ = await _queue_child(
        session,
        workflow,
        kind=kind,
        task_type=task_type,
        request_payload=refs,
        message=message,
        parent=parent,
    )
    return task, event


async def prepare_script_review_workflow(
    session: AsyncSession,
    *,
    chapter: Chapter,
    script: ScriptVersion,
    user: User,
    chat_session_id: str | None,
    source_task_id: str,
) -> tuple[DirectorWorkflowRun, AITask, TaskEvent] | None:
    existing = await session.scalar(
        select(DirectorWorkflowRun)
        .where(
            DirectorWorkflowRun.chapter_id == chapter.id,
            DirectorWorkflowRun.status.in_(ACTIVE_WORKFLOW_STATUSES),
        )
        .order_by(DirectorWorkflowRun.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return None
    workflow = DirectorWorkflowRun(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=chapter.project_id,
        chapter_id=chapter.id,
        chat_session_id=chat_session_id,
        stage=DirectorWorkflowStage.SCRIPT_REVIEWING,
        status=DirectorWorkflowStatus.RUNNING,
        script_version_id=script.id,
        context_snapshot={
            "source_hash": hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest(),
            "source_task_id": source_task_id,
            "origin": "agent_chat",
        },
        last_message="剧本已生成，正在由审核子智能体检查可拍摄性",
    )
    session.add(workflow)
    await session.flush()
    await _activate_script(session, workflow, script)
    task, event = await _queue_review(session, workflow, target="script", parent=None)
    return workflow, task, event


async def recover_orphaned_agent_script_reviews() -> int:
    from app.db.session import SessionLocal

    queued: list[tuple[AITask, TaskEvent]] = []
    recovered = 0
    async with SessionLocal() as session:
        candidates = list(
            (
                await session.scalars(
                    select(ScriptVersion)
                    .where(ScriptVersion.status == "reviewing")
                    .order_by(ScriptVersion.created_at.desc())
                )
            ).all()
        )
        seen_chapters: set[str] = set()
        for script in candidates:
            if script.chapter_id in seen_chapters:
                continue
            seen_chapters.add(script.chapter_id)
            project_file = await session.scalar(
                select(ProjectFile).where(
                    ProjectFile.project_id == script.project_id,
                    ProjectFile.kind == ProjectFileKind.SCRIPT,
                )
            )
            matching_file = None
            if project_file is not None and (
                project_file.file_metadata.get("script_version_id") == script.id
                and project_file.file_metadata.get("created_by") == "agent"
            ):
                matching_file = project_file
            if matching_file is None:
                files = list(
                    (
                        await session.scalars(
                            select(ProjectFile).where(
                                ProjectFile.project_id == script.project_id,
                                ProjectFile.kind == ProjectFileKind.SCRIPT,
                            )
                        )
                    ).all()
                )
                matching_file = next(
                    (
                        item
                        for item in files
                        if item.file_metadata.get("script_version_id") == script.id
                        and item.file_metadata.get("created_by") == "agent"
                    ),
                    None,
                )
            if matching_file is None:
                continue
            source_task_id = str(matching_file.file_metadata.get("source_task_id") or "")
            source_task = await session.get(AITask, source_task_id) if source_task_id else None
            chapter = await session.get(Chapter, script.chapter_id)
            user = await session.get(User, script.user_id)
            if (
                source_task is None
                or source_task.task_type != "agent_chat_run"
                or chapter is None
                or user is None
            ):
                continue
            chat_session_id = str(
                source_task.request_payload.get("agent_chat_session_id") or ""
            )
            if chat_session_id and await session.get(AgentChatSession, chat_session_id) is None:
                chat_session_id = ""
            prepared = await prepare_script_review_workflow(
                session,
                chapter=chapter,
                script=script,
                user=user,
                chat_session_id=chat_session_id or None,
                source_task_id=source_task.id,
            )
            if prepared is None:
                continue
            workflow, task, event = prepared
            source_task.result_payload = {
                **(source_task.result_payload or {}),
                "director_workflow_id": workflow.id,
                "director_review_task_id": task.id,
                "director_review_recovered": True,
            }
            queued.append((task, event))
            recovered += 1
        await session.commit()
    for task, event in queued:
        await enqueue_task(task.id)
        await publish_task_event(task, event)
    return recovered


async def _queue_asset_extraction(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    parent: DirectorChildRun | None,
) -> tuple[AITask, TaskEvent]:
    script = await session.get(ScriptVersion, workflow.script_version_id)
    if script is None:
        raise RuntimeError("审核通过的剧本已不存在")
    workflow.stage = DirectorWorkflowStage.ASSET_EXTRACTING
    task, event, _ = await _queue_child(
        session,
        workflow,
        kind="asset_extraction",
        task_type="chapter_asset_extraction",
        request_payload={
            "chapter_id": workflow.chapter_id,
            "script_version_id": script.id,
            "script_version": script.version,
        },
        message="剧本资产提取",
        bill_task_type="chapter_asset_extraction",
        parent=parent,
    )
    return task, event


async def _queue_storyboard(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    parent: DirectorChildRun | None = None,
) -> tuple[AITask, TaskEvent]:
    script = await session.get(ScriptVersion, workflow.script_version_id)
    extraction = await session.get(AssetExtraction, workflow.asset_extraction_id)
    if script is None or extraction is None:
        raise RuntimeError("分镜所需剧本或资产提取版本不可用")
    missing = await _missing_ready_asset_names(await _extraction_assets(session, workflow))
    if missing:
        raise RuntimeError(f"生成分镜前必须先完成资产图片：{'、'.join(missing[:8])}")
    workflow.stage = DirectorWorkflowStage.STORYBOARD_GENERATING
    task, event, _ = await _queue_child(
        session,
        workflow,
        kind="storyboard_generation",
        task_type="chapter_storyboard_generation",
        request_payload={
            "chapter_id": workflow.chapter_id,
            "script_version_id": script.id,
            "script_version": script.version,
            "asset_extraction_id": extraction.id,
        },
        message="章节分镜表生成",
        bill_task_type="chapter_storyboard_generation",
        parent=parent,
    )
    return task, event


async def _extraction_assets(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> list[Asset]:
    if not workflow.asset_extraction_id:
        return []
    return list(
        (
            await session.scalars(
                select(Asset)
                .join(AssetExtractionItem, AssetExtractionItem.asset_id == Asset.id)
                .where(AssetExtractionItem.extraction_id == workflow.asset_extraction_id)
                .order_by(Asset.asset_type, Asset.name)
            )
        ).all()
    )


async def _missing_ready_asset_names(assets: list[Asset]) -> list[str]:
    missing: list[str] = []
    for asset in assets:
        if asset.asset_type.value == "audio":
            continue
        storage_key = object_key_from_media_url(asset.media_url)
        if asset.status != AssetStatus.READY or not storage_key:
            missing.append(asset.name)
            continue
        try:
            await materialize_media_file(storage_key)
        except (FileNotFoundError, ValueError):
            missing.append(asset.name)
    return missing


async def start_storyboard_workflow(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> None:
    if workflow.stage != DirectorWorkflowStage.READY_FOR_ASSET_IMAGES:
        raise ValueError("当前流程尚未完成剧本审核与资产提取")
    assets = [
        asset
        for asset in await _extraction_assets(session, workflow)
        if asset.asset_type.value != "audio"
    ]
    if not assets:
        raise ValueError("当前资产提取版本没有可用于分镜的资产")
    queued = await _queue_asset_preparation(session, workflow, assets)
    await _commit_and_dispatch(session, queued)


async def _queue_asset_preparation(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    assets: list[Asset] | None = None,
) -> list[tuple[AITask, TaskEvent]]:
    assets = assets or [
        asset
        for asset in await _extraction_assets(session, workflow)
        if asset.asset_type.value != "audio"
    ]
    if not assets:
        raise ValueError("当前资产提取版本没有可用于分镜的资产")
    workflow.stage = DirectorWorkflowStage.ASSET_PREPARING
    workflow.status = DirectorWorkflowStatus.RUNNING
    queued: list[tuple[AITask, TaskEvent]] = []
    missing_prompt = [asset for asset in assets if not asset.generation_prompt.strip()]
    if missing_prompt:
        task, event, _ = await _queue_child(
            session,
            workflow,
            kind="asset_prompt",
            task_type="asset_prompt_generation",
            request_payload={
                "asset_ids": [asset.id for asset in missing_prompt],
                "asset_versions": {asset.id: asset.version for asset in missing_prompt},
            },
            message=f"{len(missing_prompt)} 个资产提示词生成",
            bill_task_type="asset_prompt_generation",
            quantity=len(missing_prompt),
            agent_kind=AgentKind.GENERAL,
        )
        queued.append((task, event))
    else:
        queued.extend(await _queue_missing_asset_images(session, workflow, assets))
    return queued


async def start_storyboard_workflow_for_chapter(
    session: AsyncSession,
    *,
    chapter: Chapter,
    user: User,
    chat_session_id: str | None = None,
) -> DirectorWorkflowRun:
    if not chapter.active_script_version_id:
        raise ValueError("生成分镜前必须先选择生效剧本")
    script = await session.get(ScriptVersion, chapter.active_script_version_id)
    if script is None or script.chapter_id != chapter.id or script.tenant_id != user.tenant_id:
        raise ValueError("章节生效剧本不可用")
    extraction = await session.scalar(
        select(AssetExtraction)
        .where(
            AssetExtraction.tenant_id == user.tenant_id,
            AssetExtraction.user_id == user.id,
            AssetExtraction.project_id == chapter.project_id,
            AssetExtraction.chapter_id == chapter.id,
            AssetExtraction.script_version_id == script.id,
            AssetExtraction.is_active.is_(True),
        )
        .order_by(AssetExtraction.version.desc())
        .limit(1)
    )
    if extraction is None:
        raise ValueError("生成分镜前必须先完成当前剧本的资产提取")

    existing = await session.scalar(
        select(DirectorWorkflowRun)
        .where(
            DirectorWorkflowRun.chapter_id == chapter.id,
            DirectorWorkflowRun.status.in_(ACTIVE_WORKFLOW_STATUSES),
        )
        .order_by(DirectorWorkflowRun.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        if existing.stage in {DirectorWorkflowStage.AWAITING_SCRIPT_DECISION}:
            raise ValueError("当前剧本审核还在等待处理选择，请先处理审核意见")
        if existing.stage in {DirectorWorkflowStage.AWAITING_STORYBOARD_DECISION}:
            raise ValueError("当前分镜审核还在等待处理选择，请先处理审核意见")
        if existing.stage in BUSY_STORYBOARD_STAGES:
            raise ValueError("当前章节已有进行中的导演流程")
        workflow = existing
        workflow.script_version_id = script.id
        workflow.asset_extraction_id = extraction.id
        workflow.chat_session_id = chat_session_id or workflow.chat_session_id
        workflow.stage = DirectorWorkflowStage.READY_FOR_ASSET_IMAGES
        workflow.status = DirectorWorkflowStatus.WAITING_USER
    else:
        workflow = DirectorWorkflowRun(
            tenant_id=user.tenant_id,
            user_id=user.id,
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            chat_session_id=chat_session_id,
            stage=DirectorWorkflowStage.READY_FOR_ASSET_IMAGES,
            status=DirectorWorkflowStatus.WAITING_USER,
            script_version_id=script.id,
            asset_extraction_id=extraction.id,
            context_snapshot={
                "source_hash": hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest(),
                "origin": "storyboard_start",
            },
            last_message="已确认生效剧本与资产提取版本，准备制作分镜",
        )
        session.add(workflow)
        await session.flush()

    await start_storyboard_workflow(session, workflow)
    await session.refresh(workflow)
    return workflow


async def _queue_missing_asset_images(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    assets: list[Asset] | None = None,
    parent: DirectorChildRun | None = None,
) -> list[tuple[AITask, TaskEvent]]:
    assets = assets or await _extraction_assets(session, workflow)
    missing_names = set(await _missing_ready_asset_names(assets))
    missing = [asset for asset in assets if asset.name in missing_names]
    if not missing:
        task, event = await _queue_storyboard(session, workflow, parent)
        return [(task, event)]
    project = await session.get(Project, workflow.project_id)
    if project is None:
        raise RuntimeError("导演流程项目已不存在")
    model = await resolve_image_model(
        session,
        tenant_id=workflow.tenant_id,
        resolution=project.image_resolution,
        fallback_model_id=project.image_model_id,
    )
    if model is None:
        raise RuntimeError(f"管理员尚未为 {project.image_resolution} 配置可用的图片模型")
    queued: list[tuple[AITask, TaskEvent]] = []
    for asset in missing:
        task, event, _ = await _queue_child(
            session,
            workflow,
            kind="asset_image",
            task_type="asset_image_generation",
            request_payload={
                "asset_id": asset.id,
                "asset_version": asset.version,
                "previous_asset_status": asset.status.value,
                "image_resolution": project.image_resolution,
                "aspect_ratio": project.aspect_ratio,
            },
            message=f"资产“{asset.name}”图片生成",
            model_id=model.id,
            bill_task_type="asset_image_generation",
            parent=parent,
        )
        asset.status = AssetStatus.GENERATING
        queued.append((task, event))
    return queued


async def _workflow_video_model(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> tuple[Project, AIModel, Provider]:
    project = await session.get(Project, workflow.project_id)
    if project is None:
        raise RuntimeError("导演流程项目已不存在")
    model = await session.get(AIModel, project.video_model_id) if project.video_model_id else None
    if model is None:
        model = await session.scalar(
            select(AIModel).where(
                AIModel.tenant_id == workflow.tenant_id,
                AIModel.model_type == ModelType.VIDEO,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if model is None or model.model_type != ModelType.VIDEO or not model.enabled:
        raise RuntimeError("当前项目没有可用的视频模型")
    provider = await session.get(Provider, model.provider_id)
    if provider is None or provider.tenant_id != workflow.tenant_id or not provider.enabled:
        raise RuntimeError("当前项目视频模型平台不可用")
    return project, model, provider


async def _workflow_storyboard_shots(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> tuple[StoryboardVersion, list[StoryboardShot]]:
    storyboard = await session.get(StoryboardVersion, workflow.storyboard_version_id)
    if storyboard is None or not storyboard.is_active:
        raise RuntimeError("导演流程没有可用的生效分镜")
    shots = list(
        (
            await session.scalars(
                select(StoryboardShot)
                .where(StoryboardShot.storyboard_version_id == storyboard.id)
                .order_by(StoryboardShot.order_index)
            )
        ).all()
    )
    if not shots:
        raise RuntimeError("生效分镜中没有可生成视频的镜头")
    return storyboard, shots


async def _queue_video_prompts(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    parent: DirectorChildRun | None,
    *,
    shots: list[StoryboardShot] | None = None,
) -> tuple[AITask, TaskEvent]:
    project, video_model, video_provider = await _workflow_video_model(session, workflow)
    storyboard, storyboard_shots = await _workflow_storyboard_shots(session, workflow)
    shots = shots or storyboard_shots
    resolution = str(project.video_resolution or "1080p")
    if video_model.capabilities.get("schema_version") == 1:
        resolution = compatible_video_resolution(
            video_model.capabilities,
            duration_seconds=float(shots[0].duration_seconds),
            requested_resolution=resolution,
            aspect_ratio=str(project.aspect_ratio or "16:9"),
        )
    workflow.stage = DirectorWorkflowStage.VIDEO_PROMPT_GENERATING
    task, event, _ = await _queue_child(
        session,
        workflow,
        kind="video_prompt",
        task_type="shot_video_prompt_generation",
        request_payload={
            "chapter_id": workflow.chapter_id,
            "storyboard_version_id": storyboard.id,
            "shot_ids": [shot.id for shot in shots],
            "shot_versions": {shot.id: shot.version for shot in shots},
            "overwrite": False,
            "video_resolution": resolution,
            "requested_video_resolution": project.video_resolution,
            "aspect_ratio": project.aspect_ratio,
            "audio_enabled": default_video_audio_enabled(video_model.capabilities),
            "target_video_model": {
                "provider_code": video_provider.code,
                "provider_name": video_provider.name,
                "model_id": video_model.model_id,
                "model_name": video_model.name,
                "capabilities": video_model.capabilities or {},
            },
        },
        message=f"{len(shots)} 个镜头视频提示词生成",
        bill_task_type="shot_video_prompt_generation",
        quantity=len(shots),
        parent=parent,
    )
    return task, event


async def _queue_video_generation(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    parent: DirectorChildRun | None,
) -> list[tuple[AITask, TaskEvent]]:
    project, video_model, _provider = await _workflow_video_model(session, workflow)
    storyboard, shots = await _workflow_storyboard_shots(session, workflow)
    chapter = await session.get(Chapter, workflow.chapter_id)
    if chapter is None:
        raise RuntimeError("导演流程章节已不存在")
    queued: list[tuple[AITask, TaskEvent]] = []
    workflow.stage = DirectorWorkflowStage.VIDEO_GENERATING
    ready_shot_ids = set(
        (
            await session.scalars(
                select(VideoClip.shot_id).where(
                    VideoClip.storyboard_version_id == storyboard.id,
                    VideoClip.is_active.is_(True),
                    VideoClip.status == VideoClipStatus.READY,
                    VideoClip.media_url.is_not(None),
                )
            )
        ).all()
    )
    pending_shots = [shot for shot in shots if shot.id not in ready_shot_ids]
    if not pending_shots:
        chapter.status = ChapterStatus.COMPLETED
        workflow.stage = DirectorWorkflowStage.READY_FOR_VIDEO
        workflow.status = DirectorWorkflowStatus.COMPLETED
        workflow.current_task_id = None
        workflow.last_error = None
        workflow.last_message = f"检测到 {len(shots)} 个镜头视频均已完成，无需重复生成"
        return queued
    for shot in pending_shots:
        if not shot.video_prompt.strip():
            raise RuntimeError(f"镜头 {shot.order_index:02d} 缺少视频提示词")
        latest = await session.scalar(
            select(VideoClip.version)
            .where(VideoClip.shot_id == shot.id)
            .order_by(VideoClip.version.desc())
            .limit(1)
        )
        clip = VideoClip(
            tenant_id=workflow.tenant_id,
            user_id=workflow.user_id,
            project_id=workflow.project_id,
            chapter_id=workflow.chapter_id,
            storyboard_version_id=storyboard.id,
            shot_id=shot.id,
            model_id=video_model.id,
            version=(latest or 0) + 1,
            status=VideoClipStatus.QUEUED,
        )
        session.add(clip)
        await session.flush()
        resolution = str(project.video_resolution or "1080p")
        if video_model.capabilities.get("schema_version") == 1:
            resolution = compatible_video_resolution(
                video_model.capabilities,
                duration_seconds=float(shot.duration_seconds),
                requested_resolution=resolution,
                aspect_ratio=str(project.aspect_ratio or "16:9"),
            )
        task, event, _ = await _queue_child(
            session,
            workflow,
            kind="video_generation",
            task_type="shot_video_generation",
            request_payload={
                "chapter_id": chapter.id,
                "storyboard_version_id": storyboard.id,
                "shot_id": shot.id,
                "shot_version": shot.version,
                "video_clip_id": clip.id,
                "video_resolution": resolution,
                "requested_video_resolution": project.video_resolution,
                "aspect_ratio": project.aspect_ratio,
                "duration_seconds": float(shot.duration_seconds),
                "audio_enabled": default_video_audio_enabled(video_model.capabilities),
            },
            message=f"镜头 {shot.order_index:02d} 视频生成",
            model_id=video_model.id,
            bill_task_type="shot_video_generation",
            parent=parent,
        )
        queued.append((task, event))
    chapter.status = ChapterStatus.VIDEO
    return queued


def _review_has_blocking_findings(result: dict[str, Any]) -> bool:
    review = result.get("review")
    source = review if isinstance(review, dict) else result
    findings = source.get("findings") if isinstance(source, dict) else None
    return any(
        isinstance(item, dict) and str(item.get("severity") or "").lower() == "blocking"
        for item in (findings or [])
    )


def _increment_workflow_counter(workflow: DirectorWorkflowRun, key: str) -> int:
    snapshot = dict(workflow.context_snapshot or {})
    value = int(snapshot.get(key) or 0) + 1
    snapshot[key] = value
    workflow.context_snapshot = snapshot
    return value


async def _queue_automatic_repair(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    child: DirectorChildRun,
    *,
    target: str,
    result: dict[str, Any],
) -> tuple[AITask, TaskEvent] | None:
    counter_key = "script_version_count" if target == "script" else "storyboard_version_count"
    version_count = int((workflow.context_snapshot or {}).get(counter_key) or 1)
    blocking = _review_has_blocking_findings(result)
    if version_count >= 5 and not blocking:
        target_label = "剧本" if target == "script" else "分镜"
        workflow.last_message = f"{target_label}已达到 5 个版本，记录普通问题后继续自动流程"
        return None
    is_script = target == "script"
    workflow.stage = (
        DirectorWorkflowStage.SCRIPT_REPAIRING
        if is_script
        else DirectorWorkflowStage.STORYBOARD_REPAIRING
    )
    task, event, _ = await _queue_child(
        session,
        workflow,
        kind="script_repair" if is_script else "storyboard_repair",
        task_type="director_script_repair" if is_script else "director_storyboard_repair",
        request_payload={
            "chapter_id": workflow.chapter_id,
            "repair_mode": "full" if blocking else "partial",
            "feedback": str(result.get("summary") or "请修复审核发现的问题"),
            "script_version_id": workflow.script_version_id,
            "storyboard_version_id": workflow.storyboard_version_id,
            "review": child.details,
            "automatic_review": True,
            "version_count": version_count,
        },
        message="剧本自动修复" if is_script else "分镜自动修复",
        parent=child,
    )
    return task, event


async def submit_decision(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    decision: DirectorDecisionRequest,
    *,
    option: str,
    feedback: str,
) -> None:
    if decision.resolved:
        raise ValueError("该审核选择已处理")
    if option not in {item["value"] for item in REVIEW_OPTIONS}:
        raise ValueError("不支持的审核选择")
    if option == "provide_feedback" and not feedback.strip():
        raise ValueError("请填写需要子智能体处理的具体意见")
    decision.selected_option = option
    decision.feedback = feedback.strip()
    decision.resolved = True
    parent = await session.get(DirectorChildRun, decision.child_run_id) if decision.child_run_id else None
    queued: list[tuple[AITask, TaskEvent]] = []
    is_script = workflow.stage == DirectorWorkflowStage.AWAITING_SCRIPT_DECISION
    if option == "continue_anyway":
        if is_script:
            script = await session.get(ScriptVersion, workflow.script_version_id)
            if script is None:
                raise RuntimeError("待确认剧本已不存在")
            script.status = "approved"
            queued.append(await _queue_asset_extraction(session, workflow, parent))
        else:
            workflow.stage = DirectorWorkflowStage.READY_FOR_VIDEO
            workflow.status = DirectorWorkflowStatus.COMPLETED
            workflow.current_task_id = None
            workflow.last_message = "分镜已确认，视频生成阶段暂未开放"
    else:
        kind = "script_repair" if is_script else "storyboard_repair"
        task_type = "director_script_repair" if is_script else "director_storyboard_repair"
        workflow.stage = (
            DirectorWorkflowStage.SCRIPT_REPAIRING
            if is_script
            else DirectorWorkflowStage.STORYBOARD_REPAIRING
        )
        request_payload = {
            "chapter_id": workflow.chapter_id,
            "repair_mode": "partial" if option in {"partial_repair", "provide_feedback"} else "full",
            "feedback": feedback.strip(),
            "script_version_id": workflow.script_version_id,
            "storyboard_version_id": workflow.storyboard_version_id,
            "review": parent.details if parent else {},
        }
        task, event, _ = await _queue_child(
            session,
            workflow,
            kind=kind,
            task_type=task_type,
            request_payload=request_payload,
            message="剧本修复" if is_script else "分镜修复",
            parent=parent,
        )
        queued.append((task, event))
    await _commit_and_dispatch(session, queued)


async def stop_automatic_workflow(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> None:
    if not workflow.automation_mode:
        raise ValueError("当前流程不是 AI 全自动任务")
    if workflow.status not in ACTIVE_WORKFLOW_STATUSES:
        raise ValueError("当前 AI 全自动任务已经结束")

    children = list(
        (
            await session.scalars(
                select(DirectorChildRun).where(DirectorChildRun.workflow_id == workflow.id)
            )
        ).all()
    )
    task_ids = [child.task_id for child in children if child.task_id]
    tasks = list(
        (
            await session.scalars(
                select(AITask).where(
                    AITask.id.in_(task_ids),
                    AITask.status.in_({TaskStatus.QUEUED, TaskStatus.RUNNING}),
                )
            )
        ).all()
    ) if task_ids else []
    task_by_id = {task.id: task for task in tasks}
    published: list[tuple[AITask, TaskEvent]] = []
    for child in children:
        task = task_by_id.get(child.task_id or "")
        if task is None:
            continue
        task.status = TaskStatus.CANCELLED
        task.error_message = "AI 全自动任务已由用户停止"
        child.status = DirectorChildStatus.CANCELLED
        child.summary = "已随 AI 全自动任务停止"
        if task.task_type == "asset_image_generation":
            asset = await session.get(Asset, str(task.request_payload.get("asset_id") or ""))
            if asset is not None and asset.status == AssetStatus.GENERATING:
                asset.status = AssetStatus.READY if asset.media_url else AssetStatus.PROMPT_READY
        elif task.task_type == "shot_video_generation":
            clip = await session.get(VideoClip, str(task.request_payload.get("video_clip_id") or ""))
            if clip is not None and clip.status in {
                VideoClipStatus.QUEUED,
                VideoClipStatus.GENERATING,
            }:
                clip.status = VideoClipStatus.CANCELLED
                clip.error_message = "AI 全自动任务已由用户停止"
        event = record_task_event(
            session,
            task,
            status=TaskStatus.CANCELLED,
            progress=100,
            message="AI 全自动任务已停止",
            metadata={"workflow_id": workflow.id},
        )
        await refund_task_cost(session, task, reason="停止 AI 全自动任务退款")
        published.append((task, event))

    workflow.stop_requested = True
    workflow.status = DirectorWorkflowStatus.CANCELLED
    workflow.stage = DirectorWorkflowStage.CANCELLED
    workflow.current_task_id = None
    workflow.last_error = None
    workflow.last_message = "AI 全自动制作已停止，章节已解除锁定"
    chapter = await session.get(Chapter, workflow.chapter_id)
    if chapter is not None:
        if workflow.storyboard_version_id:
            chapter.status = ChapterStatus.VIDEO if any(
                child.kind == "video_generation" and child.status == DirectorChildStatus.SUCCEEDED
                for child in children
            ) else ChapterStatus.STORYBOARD
        elif workflow.asset_extraction_id:
            chapter.status = ChapterStatus.ASSETS
        elif workflow.script_version_id:
            chapter.status = ChapterStatus.REVIEWING
        else:
            chapter.status = ChapterStatus.UNINITIALIZED
    await session.commit()
    for task, event in published:
        await publish_task_event(task, event)


async def mark_child_running(task_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        child = await session.scalar(select(DirectorChildRun).where(DirectorChildRun.task_id == task_id))
        if child is None:
            return
        workflow = await session.get(DirectorWorkflowRun, child.workflow_id)
        if (
            workflow is None
            or workflow.stop_requested
            or workflow.status not in ACTIVE_WORKFLOW_STATUSES
        ):
            return
        child.status = DirectorChildStatus.RUNNING
        workflow.current_task_id = task_id
        await session.commit()


async def _retry_child(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
    child: DirectorChildRun,
    task: AITask,
) -> tuple[AITask, TaskEvent]:
    retry_task = AITask(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        project_id=task.project_id,
        task_type=task.task_type,
        status=TaskStatus.QUEUED,
        model_id=task.model_id,
        cost=Decimal("0"),
        request_payload={key: value for key, value in task.request_payload.items() if key != "child_run_id"},
    )
    session.add(retry_task)
    await session.flush()
    retry_task.idempotency_key = retry_task.id
    retry_child = DirectorChildRun(
        tenant_id=child.tenant_id,
        user_id=child.user_id,
        project_id=child.project_id,
        workflow_id=child.workflow_id,
        parent_child_run_id=child.id,
        task_id=retry_task.id,
        kind=child.kind,
        title=child.title,
        status=DirectorChildStatus.QUEUED,
        attempt=child.attempt + 1,
        max_attempts=child.max_attempts,
        summary=f"第 {child.attempt} 次执行失败，正在自动重试",
        input_refs=child.input_refs,
    )
    session.add(retry_child)
    await session.flush()
    retry_task.request_payload = {**retry_task.request_payload, "child_run_id": retry_child.id}
    event = record_task_event(
        session,
        retry_task,
        status=TaskStatus.QUEUED,
        progress=0,
        message=f"{child.title}自动重试 {retry_child.attempt}/{retry_child.max_attempts}",
    )
    workflow.current_task_id = retry_task.id
    workflow.status = DirectorWorkflowStatus.RUNNING
    workflow.last_message = event.message
    return retry_task, event


async def on_director_task_terminal(task_id: str) -> None:
    from app.db.session import SessionLocal

    queued: list[tuple[AITask, TaskEvent]] = []
    async with SessionLocal() as session:
        task = await session.get(AITask, task_id)
        child = await session.scalar(select(DirectorChildRun).where(DirectorChildRun.task_id == task_id))
        if task is None or child is None or task.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            return
        workflow = await session.get(DirectorWorkflowRun, child.workflow_id)
        if workflow is None:
            return
        child.output_refs = dict(task.result_payload or {})
        terminal_child_status = {
            TaskStatus.SUCCEEDED: DirectorChildStatus.SUCCEEDED,
            TaskStatus.FAILED: DirectorChildStatus.FAILED,
            TaskStatus.CANCELLED: DirectorChildStatus.CANCELLED,
        }[task.status]
        if workflow.stop_requested or workflow.status == DirectorWorkflowStatus.CANCELLED:
            child.status = DirectorChildStatus.CANCELLED
            child.summary = "已随 AI 全自动任务停止"
            await session.commit()
            return
        if workflow.status in {DirectorWorkflowStatus.COMPLETED, DirectorWorkflowStatus.FAILED}:
            child.status = terminal_child_status
            child.summary = (
                str((task.result_payload or {}).get("summary") or "执行完成")
                if task.status == TaskStatus.SUCCEEDED
                else task.error_message or "子智能体执行未完成"
            )
            await session.commit()
            return
        if child.status in {
            DirectorChildStatus.SUCCEEDED,
            DirectorChildStatus.FAILED,
            DirectorChildStatus.CANCELLED,
        }:
            return
        if task.status != TaskStatus.SUCCEEDED:
            child.status = (
                DirectorChildStatus.CANCELLED
                if task.status == TaskStatus.CANCELLED
                else DirectorChildStatus.FAILED
            )
            child.summary = task.error_message or "子智能体执行未完成"
            child.details = {**child.details, "error": task.error_message or ""}
            if task.status == TaskStatus.FAILED and child.attempt < child.max_attempts:
                queued.append(await _retry_child(session, workflow, child, task))
            else:
                workflow.stage = DirectorWorkflowStage.FAILED
                workflow.status = DirectorWorkflowStatus.FAILED
                workflow.current_task_id = None
                workflow.last_error = child.summary
                workflow.last_message = f"{child.title}连续失败，请稍后重新发起"
            await _commit_and_dispatch(session, queued)
            return

        child.status = DirectorChildStatus.SUCCEEDED
        result = dict(task.result_payload or {})
        child.summary = str(
            result.get("summary") or task.request_payload.get("success_message") or "执行完成"
        )
        child.details = dict(result.get("review") or result.get("details") or {})
        workflow.current_task_id = None

        if child.kind in {"script_adaptation", "script_repair"}:
            script = await session.get(ScriptVersion, result.get("script_version_id"))
            if script is None:
                raise RuntimeError("子智能体未生成可用剧本版本")
            await _activate_script(session, workflow, script)
            if workflow.automation_mode:
                _increment_workflow_counter(workflow, "script_version_count")
            queued.append(await _queue_review(session, workflow, target="script", parent=child))
        elif child.kind == "script_review":
            approved = bool(result.get("approved"))
            script = await session.get(ScriptVersion, workflow.script_version_id)
            if script is None:
                raise RuntimeError("审核目标剧本已不存在")
            notes = str(result.get("summary") or "")
            session.add(
                ScriptReview(
                    tenant_id=workflow.tenant_id,
                    user_id=workflow.user_id,
                    project_id=workflow.project_id,
                    chapter_id=workflow.chapter_id,
                    script_version_id=script.id,
                    decision=(
                        ScriptReviewDecision.APPROVED
                        if approved
                        else ScriptReviewDecision.CHANGES_REQUESTED
                    ),
                    notes=notes,
                    reviewer_name="导演审核子智能体",
                    activated=approved,
                )
            )
            if approved:
                script.status = "approved"
                queued.append(await _queue_asset_extraction(session, workflow, child))
            elif workflow.automation_mode:
                script.status = "draft"
                repair = await _queue_automatic_repair(
                    session,
                    workflow,
                    child,
                    target="script",
                    result=result,
                )
                if repair is not None:
                    queued.append(repair)
                else:
                    script.status = "approved"
                    queued.append(await _queue_asset_extraction(session, workflow, child))
            else:
                script.status = "draft"
                workflow.stage = DirectorWorkflowStage.AWAITING_SCRIPT_DECISION
                workflow.status = DirectorWorkflowStatus.WAITING_USER
                workflow.last_message = notes or "剧本审核发现问题，请选择处理方式"
                session.add(
                    DirectorDecisionRequest(
                        tenant_id=workflow.tenant_id,
                        user_id=workflow.user_id,
                        project_id=workflow.project_id,
                        workflow_id=workflow.id,
                        child_run_id=child.id,
                        decision_type="script_review",
                        prompt=workflow.last_message,
                        options=REVIEW_OPTIONS,
                    )
                )
        elif child.kind == "asset_extraction":
            workflow.asset_extraction_id = str(result.get("extraction_id") or "") or None
            if workflow.automation_mode:
                queued.extend(await _queue_asset_preparation(session, workflow))
            else:
                workflow.stage = DirectorWorkflowStage.READY_FOR_ASSET_IMAGES
                workflow.status = DirectorWorkflowStatus.WAITING_USER
                workflow.last_message = (
                    f"已提取 {result.get('asset_count', 0)} 个资产。"
                    "请检查提示词和资产图，准备好后让导演 Agent 制作分镜。"
                )
        elif child.kind == "asset_prompt":
            assets = await _extraction_assets(session, workflow)
            queued.extend(await _queue_missing_asset_images(session, workflow, assets, child))
        elif child.kind == "asset_image":
            active_image_children = list(
                (
                    await session.scalars(
                        select(DirectorChildRun).where(
                            DirectorChildRun.workflow_id == workflow.id,
                            DirectorChildRun.kind == "asset_image",
                            DirectorChildRun.status.in_(
                                {DirectorChildStatus.QUEUED, DirectorChildStatus.RUNNING}
                            ),
                        )
                    )
                ).all()
            )
            if not active_image_children:
                assets = await _extraction_assets(session, workflow)
                missing = await _missing_ready_asset_names(assets)
                if not missing:
                    queued.append(await _queue_storyboard(session, workflow, child))
                else:
                    if workflow.automation_mode:
                        workflow.stage = DirectorWorkflowStage.FAILED
                        workflow.status = DirectorWorkflowStatus.FAILED
                        workflow.current_task_id = None
                        workflow.last_error = f"资产图片未完成：{'、'.join(missing[:8])}"
                        workflow.last_message = workflow.last_error
                    else:
                        workflow.stage = DirectorWorkflowStage.READY_FOR_ASSET_IMAGES
                        workflow.status = DirectorWorkflowStatus.WAITING_USER
                        workflow.current_task_id = None
                        workflow.last_message = f"仍有资产图片未完成：{'、'.join(missing[:8])}"
        elif child.kind in {"storyboard_generation", "storyboard_repair"}:
            workflow.storyboard_version_id = str(result.get("storyboard_version_id") or "") or None
            if workflow.automation_mode:
                _increment_workflow_counter(workflow, "storyboard_version_count")
            queued.append(await _queue_review(session, workflow, target="storyboard", parent=child))
        elif child.kind == "storyboard_review":
            approved = bool(result.get("approved"))
            if approved:
                if workflow.automation_mode:
                    queued.append(await _queue_video_prompts(session, workflow, child))
                else:
                    workflow.stage = DirectorWorkflowStage.READY_FOR_VIDEO
                    workflow.status = DirectorWorkflowStatus.COMPLETED
                    workflow.last_message = "分镜审核通过，可以开始生成镜头视频"
            elif workflow.automation_mode:
                repair = await _queue_automatic_repair(
                    session,
                    workflow,
                    child,
                    target="storyboard",
                    result=result,
                )
                if repair is not None:
                    queued.append(repair)
                else:
                    queued.append(await _queue_video_prompts(session, workflow, child))
            else:
                workflow.stage = DirectorWorkflowStage.AWAITING_STORYBOARD_DECISION
                workflow.status = DirectorWorkflowStatus.WAITING_USER
                workflow.last_message = str(result.get("summary") or "分镜审核发现问题，请选择处理方式")
                session.add(
                    DirectorDecisionRequest(
                        tenant_id=workflow.tenant_id,
                        user_id=workflow.user_id,
                        project_id=workflow.project_id,
                        workflow_id=workflow.id,
                        child_run_id=child.id,
                        decision_type="storyboard_review",
                        prompt=workflow.last_message,
                        options=REVIEW_OPTIONS,
                    )
                )
        elif child.kind == "video_prompt":
            queued.extend(await _queue_video_generation(session, workflow, child))
        elif child.kind == "video_generation":
            active_video_children = list(
                (
                    await session.scalars(
                        select(DirectorChildRun).where(
                            DirectorChildRun.workflow_id == workflow.id,
                            DirectorChildRun.kind == "video_generation",
                            DirectorChildRun.status.in_(
                                {DirectorChildStatus.QUEUED, DirectorChildStatus.RUNNING}
                            ),
                        )
                    )
                ).all()
            )
            if not active_video_children:
                storyboard, shots = await _workflow_storyboard_shots(session, workflow)
                ready_shot_ids = set(
                    (
                        await session.scalars(
                            select(VideoClip.shot_id).where(
                                VideoClip.storyboard_version_id == storyboard.id,
                                VideoClip.is_active.is_(True),
                                VideoClip.status == VideoClipStatus.READY,
                            )
                        )
                    ).all()
                )
                missing_shots = [shot for shot in shots if shot.id not in ready_shot_ids]
                if missing_shots:
                    workflow.stage = DirectorWorkflowStage.FAILED
                    workflow.status = DirectorWorkflowStatus.FAILED
                    workflow.last_error = "部分镜头没有生成可用视频"
                    workflow.last_message = workflow.last_error
                else:
                    chapter = await session.get(Chapter, workflow.chapter_id)
                    if chapter is not None:
                        chapter.status = ChapterStatus.COMPLETED
                    workflow.stage = DirectorWorkflowStage.READY_FOR_VIDEO
                    workflow.status = DirectorWorkflowStatus.COMPLETED
                    workflow.current_task_id = None
                    workflow.last_message = f"AI 全自动制作完成，共生成 {len(shots)} 个镜头视频"
            else:
                workflow.current_task_id = next(
                    (
                        active_child.task_id
                        for active_child in active_video_children
                        if active_child.task_id
                    ),
                    None,
                )
        await _commit_and_dispatch(session, queued)


async def recover_automatic_workflows() -> int:
    """Reconcile active automatic workflows after a Worker restart."""
    from app.db.session import SessionLocal

    terminal_task_ids: list[str] = []
    repaired_workflows = 0
    async with SessionLocal() as session:
        workflows = list(
            (
                await session.scalars(
                    select(DirectorWorkflowRun).where(
                        DirectorWorkflowRun.automation_mode.is_(True),
                        DirectorWorkflowRun.status.in_(ACTIVE_WORKFLOW_STATUSES),
                    )
                )
            ).all()
        )
        for workflow in workflows:
            children = list(
                (
                    await session.scalars(
                        select(DirectorChildRun)
                        .where(DirectorChildRun.workflow_id == workflow.id)
                        .order_by(DirectorChildRun.created_at)
                    )
                ).all()
            )
            pending_children = [
                child
                for child in children
                if child.status in {DirectorChildStatus.QUEUED, DirectorChildStatus.RUNNING}
            ]
            task_ids = [child.task_id for child in pending_children if child.task_id]
            tasks = list(
                (
                    await session.scalars(select(AITask).where(AITask.id.in_(task_ids)))
                ).all()
            ) if task_ids else []
            tasks_by_id = {task.id: task for task in tasks}
            active_task_id: str | None = None
            invalid_child: DirectorChildRun | None = None
            for child in pending_children:
                task = tasks_by_id.get(child.task_id or "")
                if task is None:
                    invalid_child = child
                    break
                if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    terminal_task_ids.append(task.id)
                elif task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING} and active_task_id is None:
                    active_task_id = task.id
            if invalid_child is not None:
                invalid_child.status = DirectorChildStatus.FAILED
                invalid_child.summary = "Worker 恢复时未找到对应任务"
                workflow.stage = DirectorWorkflowStage.FAILED
                workflow.status = DirectorWorkflowStatus.FAILED
                workflow.current_task_id = None
                workflow.last_error = invalid_child.summary
                workflow.last_message = invalid_child.summary
                repaired_workflows += 1
            elif active_task_id and workflow.current_task_id != active_task_id:
                workflow.current_task_id = active_task_id
                repaired_workflows += 1
        await session.commit()

    for task_id in dict.fromkeys(terminal_task_ids):
        await on_director_task_terminal(task_id)
    return repaired_workflows + len(set(terminal_task_ids))
