from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AITask, Notification, TaskEvent, TaskStatus
from app.services.task_queue import publish_user_event

TERMINAL_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
TASK_TITLES = {
    "agent_chat_run": "Agent 创作回复",
    "project_cover_generation": "项目封面生成",
    "chapter_analysis_generation": "章节分析",
    "chapter_script_generation": "AI 剧本生成",
    "chapter_asset_extraction": "剧本资产提取",
    "asset_prompt_generation": "资产提示词生成",
    "asset_image_generation": "资产图片生成",
    "chapter_storyboard_generation": "章节分镜生成",
    "shot_video_prompt_generation": "镜头视频提示词",
    "shot_video_generation": "镜头视频生成",
    "chapter_dialogue_extraction": "章节台词提取",
    "dialogue_tts_generation": "角色台词配音",
    "chapter_composition_render": "章节成片渲染",
}


def task_title(task: AITask) -> str:
    if task.task_type == "agent_chat_run" and task.request_payload.get("scope") == "personal":
        return {
            "image": "个人图片生成",
            "video": "个人视频生成",
            "skill": "个人 Skill 创作",
        }.get(str(task.request_payload.get("mode") or ""), "个人 Agent 回复")
    return TASK_TITLES.get(task.task_type, "AI 任务")


def record_task_event(
    session: AsyncSession,
    task: AITask,
    *,
    status: TaskStatus,
    progress: int,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> TaskEvent:
    now = datetime.now(UTC)
    if status == TaskStatus.QUEUED:
        task.worker_id = None
        task.lease_expires_at = None
        task.heartbeat_at = None
        task.started_at = None
        task.completed_at = None
    elif status in TERMINAL_STATUSES:
        task.worker_id = None
        task.lease_expires_at = None
        task.completed_at = now
    event = TaskEvent(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        task_id=task.id,
        status=status,
        progress=max(0, min(100, progress)),
        message=message,
        event_metadata=metadata or {},
    )
    session.add(event)
    if status in TERMINAL_STATUSES and not task.request_payload.get("internal"):
        title = task_title(task)
        suffix = {
            TaskStatus.SUCCEEDED: "已完成",
            TaskStatus.FAILED: "失败",
            TaskStatus.CANCELLED: "已取消",
        }[status]
        session.add(
            Notification(
                tenant_id=task.tenant_id,
                user_id=task.user_id,
                project_id=task.project_id,
                task_id=task.id,
                category="task",
                title=f"{title}{suffix}",
                message=message,
                notification_metadata={"status": status.value, "task_type": task.task_type},
            )
        )
    return event


async def publish_task_event(task: AITask, event: TaskEvent) -> None:
    if task.request_payload.get("internal"):
        return
    await publish_user_event(
        task.user_id,
        {
            "type": "task.updated",
            "task_id": task.id,
            "project_id": task.project_id,
            "status": event.status.value,
            "progress": event.progress,
            "message": event.message,
            "created_at": event.created_at,
        },
    )


async def publish_agent_stream_event(
    *,
    user_id: str,
    task_id: str,
    project_id: str | None,
    session_id: str,
    event: dict[str, Any],
) -> bool | None:
    event_type = str(event.get("type") or "")
    payload: dict[str, Any] = {
        "type": "agent.stream",
        "task_id": task_id,
        "project_id": project_id,
        "session_id": session_id,
        "created_at": event.get("created_at") or datetime.now(UTC),
    }
    if event_type == "TEXT_BLOCK_DELTA":
        delta = str(event.get("delta") or "")
        if not delta:
            return None
        payload.update({"event": "text.delta", "delta": delta})
    elif event_type == "MODEL_CALL_START":
        payload.update(
            {
                "event": "model.start",
                "phase": "thinking",
                "step_id": str(event.get("_cineforge_step_id") or event.get("model_call_id") or ""),
                "step_state": "running",
            }
        )
    elif event_type == "MODEL_CALL_END":
        payload.update(
            {
                "event": "model.end",
                "step_id": str(event.get("_cineforge_step_id") or event.get("model_call_id") or ""),
                "step_state": str(event.get("_cineforge_step_state") or "succeeded"),
            }
        )
    elif event_type == "THINKING_BLOCK_START":
        payload.update({"event": "phase", "phase": "thinking"})
    elif event_type == "TOOL_CALL_START":
        payload.update(
            {
                "event": "tool.start",
                "tool_name": str(event.get("tool_call_name") or "Tool"),
                "step_id": str(event.get("_cineforge_step_id") or event.get("tool_call_id") or ""),
                "step_state": "running",
            }
        )
    elif event_type == "TOOL_RESULT_END":
        payload.update(
            {
                "event": "tool.end",
                "tool_name": str(event.get("_cineforge_tool_name") or "Tool"),
                "step_id": str(event.get("_cineforge_step_id") or event.get("tool_call_id") or ""),
                "step_state": str(event.get("_cineforge_step_state") or event.get("state") or "succeeded"),
            }
        )
    elif event_type == "REPLY_END":
        payload.update({"event": "phase", "phase": "finalizing"})
    else:
        return None
    return await publish_user_event(user_id, payload)
