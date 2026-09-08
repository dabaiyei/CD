from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import (
    AgentChatMessage,
    AgentChatSession,
    AgentProfile,
    AITask,
    Asset,
    AssetExtraction,
    AssetStatus,
    AudioClip,
    AudioClipStatus,
    Chapter,
    ChapterAnalysis,
    ChapterStatus,
    CompositionStatus,
    CompositionVersion,
    DialogueLine,
    DialogueVersion,
    Project,
    ScriptVersion,
    StoryboardShot,
    StoryboardVersion,
    TaskEvent,
    TaskStatus,
    User,
    VideoClip,
    VideoClipStatus,
    VoiceBinding,
)
from app.db.session import get_session
from app.domain.schemas import TaskEventPublic, TaskPage, TaskPublic
from app.services.billing import debit_task_cost, refund_task_cost
from app.services.task_events import publish_task_event, record_task_event
from app.services.task_queue import enqueue_task

router = APIRouter(prefix="/tasks", tags=["ai-tasks"])
TERMINAL_TASK_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def task_public(task: AITask, event: TaskEvent | None = None) -> TaskPublic:
    payload = TaskPublic.model_validate(task)
    if event is None:
        return payload
    return payload.model_copy(
        update={
            "progress": event.progress,
            "latest_message": event.message,
            "latest_event_at": event.created_at,
        }
    )


async def latest_events_for_tasks(
    session: AsyncSession,
    task_ids: list[str],
) -> dict[str, TaskEvent]:
    if not task_ids:
        return {}
    ranked_events = (
        select(
            TaskEvent.id.label("event_id"),
            func.row_number()
            .over(
                partition_by=TaskEvent.task_id,
                order_by=(TaskEvent.created_at.desc(), TaskEvent.id.desc()),
            )
            .label("event_rank"),
        )
        .where(TaskEvent.task_id.in_(task_ids))
        .subquery()
    )
    events = list(
        (
            await session.scalars(
                select(TaskEvent)
                .join(ranked_events, ranked_events.c.event_id == TaskEvent.id)
                .where(ranked_events.c.event_rank == 1)
            )
        ).all()
    )
    return {event.task_id: event for event in events}


async def task_public_with_latest_event(session: AsyncSession, task: AITask) -> TaskPublic:
    events = await latest_events_for_tasks(session, [task.id])
    return task_public(task, events.get(task.id))


async def task_for_user(
    session: AsyncSession,
    task_id: str,
    user: User,
    *,
    for_update: bool = False,
) -> AITask:
    query = select(AITask).where(
        AITask.id == task_id,
        AITask.tenant_id == user.tenant_id,
        AITask.user_id == user.id,
    )
    if for_update:
        query = query.with_for_update()
    task = await session.scalar(query)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


async def restore_chapter_status(session: AsyncSession, task: AITask) -> None:
    chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
    if chapter is None or chapter.user_id != task.user_id:
        return
    script_count = await session.scalar(
        select(func.count(ScriptVersion.id)).where(
            ScriptVersion.chapter_id == chapter.id,
            ScriptVersion.user_id == task.user_id,
        )
    )
    analysis_count = await session.scalar(
        select(func.count(ChapterAnalysis.id)).where(
            ChapterAnalysis.chapter_id == chapter.id,
            ChapterAnalysis.user_id == task.user_id,
        )
    )
    if script_count:
        chapter.status = ChapterStatus.REVIEWING
    elif analysis_count:
        chapter.status = ChapterStatus.ANALYZED
    else:
        chapter.status = ChapterStatus.UNINITIALIZED


@router.get("", response_model=TaskPage)
async def list_tasks(
    project_id: str | None = None,
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    before: datetime | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskPage:
    query = select(AITask).where(
        AITask.tenant_id == user.tenant_id,
        AITask.user_id == user.id,
        AITask.task_type != "agent_memory_maintenance",
    )
    if project_id:
        query = query.where(AITask.project_id == project_id)
    if task_status:
        query = query.where(AITask.status == task_status)
    if before:
        query = query.where(AITask.created_at < before)
    rows = list((await session.scalars(query.order_by(AITask.created_at.desc()).limit(limit + 1))).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    latest_events = await latest_events_for_tasks(session, [item.id for item in items])
    return TaskPage(
        items=[task_public(item, latest_events.get(item.id)) for item in items],
        next_before=items[-1].created_at if has_more and items else None,
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_tasks(
    group: Literal["terminal", "failed", "completed"] = Query(default="terminal"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    statuses = {
        "terminal": TERMINAL_TASK_STATUSES,
        "failed": {TaskStatus.FAILED, TaskStatus.CANCELLED},
        "completed": {TaskStatus.SUCCEEDED},
    }[group]
    await session.execute(
        delete(AITask).where(
            AITask.tenant_id == user.tenant_id,
            AITask.user_id == user.id,
            AITask.status.in_(statuses),
            AITask.task_type != "agent_memory_maintenance",
        )
    )
    await session.commit()


@router.get("/{task_id}", response_model=TaskPublic)
async def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskPublic:
    task = await task_for_user(session, task_id, user)
    return await task_public_with_latest_event(session, task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    task = await task_for_user(session, task_id, user, for_update=True)
    if task.status not in TERMINAL_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="只有已完成、失败或已取消的任务可以删除")
    await session.delete(task)
    await session.commit()


@router.get("/{task_id}/events", response_model=list[TaskEventPublic])
async def task_events(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TaskEvent]:
    await task_for_user(session, task_id, user)
    return list(
        (
            await session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id)
                .order_by(TaskEvent.created_at, TaskEvent.id)
            )
        ).all()
    )


@router.post("/{task_id}/cancel", response_model=TaskPublic)
async def cancel_task(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskPublic:
    task = await task_for_user(session, task_id, user, for_update=True)
    if task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
        raise HTTPException(status_code=409, detail="只有排队中或运行中的任务可以取消")
    if task.status == TaskStatus.RUNNING and task.task_type not in {"agent_chat_run", "project_ai_creation"}:
        raise HTTPException(status_code=409, detail="当前仅支持停止运行中的 Agent 对话任务")
    was_running = task.status == TaskStatus.RUNNING
    task.status = TaskStatus.CANCELLED
    task.error_message = "用户取消任务"
    if task.task_type == "asset_image_generation":
        asset = await session.get(Asset, str(task.request_payload.get("asset_id") or ""))
        if asset is not None and asset.user_id == task.user_id and asset.status == AssetStatus.GENERATING:
            asset.status = AssetStatus.READY if asset.media_url else AssetStatus.PROMPT_READY
    elif task.task_type == "shot_video_generation":
        clip = await session.get(VideoClip, str(task.request_payload.get("video_clip_id") or ""))
        if clip is not None and clip.user_id == task.user_id and clip.status == VideoClipStatus.QUEUED:
            clip.status = VideoClipStatus.CANCELLED
            clip.error_message = "用户取消任务"
    elif task.task_type == "dialogue_tts_generation":
        audio_clip = await session.get(
            AudioClip,
            str(task.request_payload.get("audio_clip_id") or ""),
        )
        if (
            audio_clip is not None
            and audio_clip.user_id == task.user_id
            and audio_clip.status == AudioClipStatus.QUEUED
        ):
            audio_clip.status = AudioClipStatus.CANCELLED
            audio_clip.error_message = "用户取消任务"
    elif task.task_type == "chapter_composition_render":
        composition = await session.get(
            CompositionVersion,
            str(task.request_payload.get("composition_id") or ""),
        )
        if composition is not None and composition.status == CompositionStatus.RENDERING:
            composition.status = CompositionStatus.DRAFT
            composition.error_message = "用户取消渲染"
    elif task.task_type in {"chapter_analysis_generation", "chapter_script_generation"}:
        await restore_chapter_status(session, task)
    event = record_task_event(
        session,
        task,
        status=TaskStatus.CANCELLED,
        progress=100,
        message="Agent 生成已停止" if was_running else "任务已由用户取消",
        metadata={"cancelled_while_running": was_running},
    )
    await refund_task_cost(session, task, reason="取消 AI 任务退款")
    await session.commit()
    await session.refresh(task)
    await publish_task_event(task, event)
    return await task_public_with_latest_event(session, task)


@router.post("/{task_id}/retry", response_model=TaskPublic, status_code=status.HTTP_202_ACCEPTED)
async def retry_task(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskPublic:
    task = await task_for_user(session, task_id, user, for_update=True)
    if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
        raise HTTPException(status_code=409, detail="只有失败或已取消的任务可以重试")
    if task.task_type == "project_ai_creation":
        project = await session.get(Project, task.project_id)
        if project is None or project.creation_state.get("task_id") != task.id:
            raise HTTPException(status_code=409, detail="此创作任务已被替代，请从项目当前创作进度继续")
    clip: VideoClip | None = None
    if task.task_type in {"chapter_analysis_generation", "chapter_script_generation"}:
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        if chapter is None or chapter.user_id != task.user_id:
            raise HTTPException(status_code=409, detail="章节已不存在，不能重试")
        source_hash = hashlib.sha256(chapter.original_content.encode("utf-8")).hexdigest()
        if source_hash != task.request_payload.get("source_hash"):
            raise HTTPException(status_code=409, detail="章节原文已更新，不能重试旧任务")
        if task.task_type == "chapter_script_generation":
            analysis_id = str(task.request_payload.get("analysis_id") or "")
            base_script_id = str(task.request_payload.get("base_script_version_id") or "")
            analysis = await session.get(ChapterAnalysis, analysis_id) if analysis_id else None
            base_script = await session.get(ScriptVersion, base_script_id) if base_script_id else None
            if analysis_id and (
                analysis is None or analysis.chapter_id != chapter.id or analysis.user_id != task.user_id
            ):
                raise HTTPException(status_code=409, detail="章节分析版本已失效，不能重试")
            if base_script_id and (
                base_script is None
                or base_script.chapter_id != chapter.id
                or base_script.user_id != task.user_id
            ):
                raise HTTPException(status_code=409, detail="参考剧本版本已失效，不能重试")
    elif task.task_type == "agent_chat_run":
        chat_session = await session.get(
            AgentChatSession,
            str(task.request_payload.get("agent_chat_session_id") or ""),
        )
        user_message = await session.get(
            AgentChatMessage,
            str(task.request_payload.get("user_message_id") or ""),
        )
        agent = await session.get(
            AgentProfile,
            str(task.request_payload.get("agent_profile_id") or ""),
        )
        if (
            chat_session is None
            or chat_session.project_id != task.project_id
            or chat_session.user_id != task.user_id
            or user_message is None
            or user_message.session_id != chat_session.id
            or user_message.run_id != task.id
        ):
            raise HTTPException(status_code=409, detail="Agent 会话或用户消息已失效，不能重试")
        prompt_hash = hashlib.sha256(user_message.content.encode("utf-8")).hexdigest()
        if prompt_hash != task.request_payload.get("prompt_hash"):
            raise HTTPException(status_code=409, detail="对话内容已更新，不能重试旧任务")
        if (
            agent is None
            or not agent.enabled
            or agent.version != int(task.request_payload.get("agent_version") or -1)
        ):
            raise HTTPException(status_code=409, detail="Agent 配置已更新，请重新发送本轮消息")
        await session.execute(
            delete(AgentChatMessage).where(
                AgentChatMessage.session_id == chat_session.id,
                AgentChatMessage.run_id == task.id,
                AgentChatMessage.finish_reason == "failed",
            )
        )
    elif task.task_type == "chapter_storyboard_generation":
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        extraction = await session.get(
            AssetExtraction,
            str(task.request_payload.get("asset_extraction_id") or ""),
        )
        if (
            chapter is None
            or chapter.user_id != task.user_id
            or chapter.active_script_version_id != task.request_payload.get("script_version_id")
            or extraction is None
            or extraction.user_id != task.user_id
            or not extraction.is_active
            or extraction.script_version_id != chapter.active_script_version_id
        ):
            raise HTTPException(status_code=409, detail="剧本或资产提取版本已切换，不能重试旧分镜任务")
    elif task.task_type == "shot_video_generation":
        clip = await session.get(VideoClip, str(task.request_payload.get("video_clip_id") or ""))
        shot = await session.get(StoryboardShot, str(task.request_payload.get("shot_id") or ""))
        storyboard = await session.get(
            StoryboardVersion,
            str(task.request_payload.get("storyboard_version_id") or ""),
        )
        if (
            clip is None
            or clip.user_id != task.user_id
            or shot is None
            or shot.user_id != task.user_id
            or storyboard is None
            or storyboard.user_id != task.user_id
            or not storyboard.is_active
        ):
            raise HTTPException(status_code=409, detail="镜头或分镜已失效，不能重试视频任务")
        if shot.storyboard_version_id != storyboard.id:
            raise HTTPException(status_code=409, detail="镜头不再属于当前分镜，不能重试")
        if shot.version != int(task.request_payload.get("shot_version") or -1):
            raise HTTPException(status_code=409, detail="镜头内容已更新，不能重试旧视频任务")
    elif task.task_type == "chapter_dialogue_extraction":
        chapter = await session.get(Chapter, str(task.request_payload.get("chapter_id") or ""))
        script = await session.get(
            ScriptVersion,
            str(task.request_payload.get("script_version_id") or ""),
        )
        storyboard_id = str(task.request_payload.get("storyboard_version_id") or "")
        storyboard = await session.get(StoryboardVersion, storyboard_id) if storyboard_id else None
        if (
            chapter is None
            or chapter.user_id != task.user_id
            or script is None
            or script.user_id != task.user_id
            or chapter.active_script_version_id != script.id
        ):
            raise HTTPException(status_code=409, detail="生效剧本已切换，不能重试旧台词提取任务")
        if hashlib.sha256(script.content.encode("utf-8")).hexdigest() != task.request_payload.get(
            "script_hash"
        ):
            raise HTTPException(status_code=409, detail="剧本内容已更新，不能重试旧台词提取任务")
        if storyboard_id and (
            storyboard is None or storyboard.user_id != task.user_id or not storyboard.is_active
        ):
            raise HTTPException(status_code=409, detail="生效分镜已切换，不能重试旧台词提取任务")
    elif task.task_type == "dialogue_tts_generation":
        audio_clip = await session.get(
            AudioClip,
            str(task.request_payload.get("audio_clip_id") or ""),
        )
        dialogue = await session.get(
            DialogueVersion,
            str(task.request_payload.get("dialogue_version_id") or ""),
        )
        line = await session.get(
            DialogueLine,
            str(task.request_payload.get("dialogue_line_id") or ""),
        )
        binding = await session.get(
            VoiceBinding,
            str(task.request_payload.get("voice_binding_id") or ""),
        )
        if (
            audio_clip is None
            or audio_clip.user_id != task.user_id
            or dialogue is None
            or dialogue.user_id != task.user_id
            or not dialogue.is_active
            or line is None
            or line.user_id != task.user_id
            or binding is None
            or binding.user_id != task.user_id
            or not binding.enabled
        ):
            raise HTTPException(status_code=409, detail="台词版本或音色绑定已失效，不能重试配音")
        if line.version != int(task.request_payload.get("line_version") or -1) or binding.version != int(
            task.request_payload.get("binding_version") or -1
        ):
            raise HTTPException(status_code=409, detail="台词或音色已更新，不能重试旧配音任务")
    elif task.task_type == "chapter_composition_render":
        composition = await session.get(
            CompositionVersion,
            str(task.request_payload.get("composition_id") or ""),
        )
        storyboard = (
            await session.get(StoryboardVersion, composition.storyboard_version_id) if composition else None
        )
        if (
            composition is None
            or composition.user_id != task.user_id
            or composition.status not in {CompositionStatus.DRAFT, CompositionStatus.FAILED}
            or storyboard is None
            or storyboard.user_id != task.user_id
            or not storyboard.is_active
        ):
            raise HTTPException(status_code=409, detail="成片清单或生效分镜已失效，不能重试")
    await debit_task_cost(session, task, reason="重试 AI 任务")
    result = dict(task.result_payload or {})
    provider_job_id = str(task.provider_job_id or result.get("provider_job_id") or "") or None
    resume_provider_job = bool(result.get("resume_provider_job") and provider_job_id)
    result["credit_refunded"] = False
    result["retry_count"] = int(result.get("retry_count") or 0) + 1
    task.result_payload = result
    task.status = TaskStatus.QUEUED
    task.error_message = None
    if (
        task.task_type == "agent_chat_run"
        and task.request_payload.get("scope") == "personal"
        and task.request_payload.get("mode") in {"image", "video"}
        and not resume_provider_job
    ):
        result.pop("provider_job_id", None)
        result.pop("generated_media_draft", None)
        task.provider_job_id = None
        task.result_payload = result
    if task.task_type == "chapter_analysis_generation":
        chapter.status = ChapterStatus.ANALYZING
    elif task.task_type == "chapter_script_generation":
        chapter.status = ChapterStatus.SCRIPTING
    elif task.task_type == "asset_image_generation":
        asset = await session.get(Asset, str(task.request_payload.get("asset_id") or ""))
        if asset is None or asset.user_id != task.user_id or not asset.generation_prompt.strip():
            raise HTTPException(status_code=409, detail="资产已不存在或提示词已失效")
        task.request_payload = {**task.request_payload, "asset_version": asset.version}
        asset.status = AssetStatus.GENERATING
    elif task.task_type == "shot_video_generation" and clip is not None:
        if not resume_provider_job:
            result.pop("provider_job_id", None)
            task.provider_job_id = None
        result.pop("media_url", None)
        task.result_payload = result
        clip.status = VideoClipStatus.GENERATING if resume_provider_job else VideoClipStatus.QUEUED
        if resume_provider_job:
            task.provider_job_id = provider_job_id
        clip.provider_job_id = provider_job_id if resume_provider_job else None
        clip.error_message = None
    elif task.task_type == "dialogue_tts_generation":
        if not resume_provider_job:
            result.pop("provider_job_id", None)
            task.provider_job_id = None
        result.pop("media_url", None)
        task.result_payload = result
        audio_clip.line_version = line.version
        audio_clip.binding_version = binding.version
        audio_clip.status = AudioClipStatus.GENERATING if resume_provider_job else AudioClipStatus.QUEUED
        if resume_provider_job:
            task.provider_job_id = provider_job_id
        audio_clip.provider_job_id = provider_job_id if resume_provider_job else None
        audio_clip.error_message = None
    elif task.task_type == "chapter_composition_render":
        composition.status = CompositionStatus.RENDERING
        composition.error_message = None
    event = record_task_event(
        session,
        task,
        status=TaskStatus.QUEUED,
        progress=0,
        message=("任务已重新进入队列，将继续查询服务商任务" if resume_provider_job else "任务已重新进入队列"),
        metadata={
            "retry_count": result["retry_count"],
            "resume_provider_job": resume_provider_job,
        },
    )
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return await task_public_with_latest_event(session, task)
