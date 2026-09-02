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
    ScriptReview,
    ScriptReviewDecision,
    ScriptVersion,
    TaskEvent,
    TaskStatus,
    User,
)
from app.services.billing import resolve_task_pricing
from app.services.composition import invalidate_compositions
from app.services.image_model_routing import resolve_image_model
from app.services.object_storage import materialize_media_file, object_key_from_media_url
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
}


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
) -> tuple[AgentProfile, AIModel]:
    agent = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.kind == kind,
            AgentProfile.enabled.is_(True),
        )
    )
    if agent is None or not agent.text_model_id:
        raise RuntimeError("管理员尚未配置可用的导演 Agent 与文本模型")
    model = await session.get(AIModel, agent.text_model_id)
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
        agent, model = await _agent_and_model(session, workflow.tenant_id, kind=agent_kind)
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
        max_attempts=3,
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
) -> DirectorWorkflowRun:
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
        context_snapshot={
            "source_hash": hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest(),
            "analysis_id": analysis.id if analysis else None,
            "instruction": instruction,
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
    parent: DirectorChildRun,
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
    await _commit_and_dispatch(session, queued)


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


async def mark_child_running(task_id: str) -> None:
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        child = await session.scalar(select(DirectorChildRun).where(DirectorChildRun.task_id == task_id))
        if child is None:
            return
        child.status = DirectorChildStatus.RUNNING
        workflow = await session.get(DirectorWorkflowRun, child.workflow_id)
        if workflow is not None:
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
                    workflow.stage = DirectorWorkflowStage.READY_FOR_ASSET_IMAGES
                    workflow.status = DirectorWorkflowStatus.WAITING_USER
                    workflow.current_task_id = None
                    workflow.last_message = f"仍有资产图片未完成：{'、'.join(missing[:8])}"
        elif child.kind in {"storyboard_generation", "storyboard_repair"}:
            workflow.storyboard_version_id = str(result.get("storyboard_version_id") or "") or None
            queued.append(await _queue_review(session, workflow, target="storyboard", parent=child))
        elif child.kind == "storyboard_review":
            approved = bool(result.get("approved"))
            if approved:
                workflow.stage = DirectorWorkflowStage.READY_FOR_VIDEO
                workflow.status = DirectorWorkflowStatus.COMPLETED
                workflow.last_message = "分镜审核通过，视频生成阶段暂未开放"
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
        await _commit_and_dispatch(session, queued)
