from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.director import screenplay_agent
from app.api.routes.projects import project_for_user, validate_project_relations
from app.db.models import AITask, Project, TaskEvent, TaskStatus, User
from app.db.session import get_session
from app.services.ai_creation import CreationAction, save_creation_file
from app.services.billing import resolve_task_pricing
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import create_queued_task, serialize_project_task_submissions

router = APIRouter(prefix="/projects/{project_id}/ai-creation", tags=["AI creation"])


@router.get("")
async def read_creation(
    project_id: str, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> dict:
    project = await project_for_user(session, project_id, user)
    state = dict(project.creation_state or {})
    task = await session.get(AITask, state.get("task_id")) if state.get("task_id") else None
    latest = (
        await session.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id)
            .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
            .limit(1)
        )
        if task
        else None
    )
    missing_task = task is None and state.get("phase") in {"proposals", "outline"}
    state.pop("checkpoint", None)
    return {
        "state": state,
        "task_status": task.status.value if task else "failed" if missing_task else None,
        "error": task.error_message
        if task
        else "任务记录已清理，可从已保存进度继续"
        if missing_task
        else None,
        "progress": latest.progress if latest else 0,
        "message": latest.message if latest else "",
    }


@router.post("")
async def advance_creation(
    project_id: str,
    payload: CreationAction,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    project = await project_for_user(session, project_id, user)
    await serialize_project_task_submissions(session, project_id)
    await session.refresh(project)
    if project.creation_mode != "ai":
        raise HTTPException(status_code=409, detail="此项目不是 AI 创作项目")
    state = dict(project.creation_state or {})
    if payload.revision != state.get("revision", 0):
        raise HTTPException(status_code=409, detail="创作进度已更新，请刷新后继续")
    task = await session.get(AITask, state.get("task_id")) if state.get("task_id") else None
    if task and task.status in {TaskStatus.RUNNING, TaskStatus.QUEUED}:
        raise HTTPException(status_code=409, detail="创作任务仍在进行中")
    if state.get("phase") == "ready":
        raise HTTPException(status_code=409, detail="项目大纲和章节已完成")
    # SQLite ignores SELECT FOR UPDATE; compare-and-swap prevents double charging there too.
    claimed = await session.execute(
        update(Project)
        .where(
            Project.id == project.id,
            func.coalesce(Project.creation_state["revision"].as_integer(), 0) == payload.revision,
        )
        .values(creation_state={**state, "revision": payload.revision + 1})
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise HTTPException(status_code=409, detail="创作进度已更新，请刷新后继续")
    history = list(state.get("messages", []))
    creation_checkpoint = None
    if payload.action != "retry":
        state.pop("checkpoint", None)
    if payload.action == "propose":
        if not payload.preferences:
            raise HTTPException(status_code=422, detail="请填写类型、章节数量及每章目标时长")
        state["preferences"] = payload.preferences.model_dump()
        state.pop("selected", None)
        phase = "proposals"
        history.append({"role": "user", "content": payload.preferences.model_dump()})
    elif payload.action == "choose":
        choices = state.get("proposals", [])
        if payload.proposal_index is None or payload.proposal_index >= len(choices):
            raise HTTPException(status_code=422, detail="请选择当前提案")
        state["selected"] = choices[payload.proposal_index]
        phase = "outline"
        history.append({"role": "user", "content": {"selected": state["selected"]}})
    else:
        if (task and task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED}) or (
            task is None and state.get("phase") not in {"proposals", "outline"}
        ):
            raise HTTPException(status_code=409, detail="没有可重试的创作任务")
        phase = str(state.get("phase") or "proposals")
        creation_checkpoint = (
            (task.result_payload or {}).get("creation_checkpoint") if task else state.get("checkpoint")
        )
    await validate_project_relations(session, user.tenant_id, {"text_model_id": project.text_model_id})
    agent = await screenplay_agent(session, user.tenant_id)
    pricing = await resolve_task_pricing(session, tenant_id=user.tenant_id, task_type="project_ai_creation")
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project.id,
        task_type="project_ai_creation",
        model_id=project.text_model_id,
        cost=pricing.total_cost,
        request_payload={
            "agent_profile_id": agent.id,
            "phase": phase,
            "preferences": state["preferences"],
            "selected": state.get("selected"),
            "previous_proposals": state.get("proposals", []),
            "feedback_history": [
                message["content"]["feedback"]
                for message in history
                if message.get("role") == "user"
                and isinstance(message.get("content"), dict)
                and message["content"].get("feedback")
            ][-12:],
            "creation_checkpoint": creation_checkpoint,
            "pricing": pricing.as_payload(),
        },
        message="创作剧集提案" if phase == "proposals" else "规划故事大纲与章节",
    )
    state.update(phase=phase, task_id=task.id, revision=state.get("revision", 0) + 1, messages=history)
    project.creation_state = state
    await save_creation_file(
        session, project, "AI创作对话记录.json", json.dumps(state, ensure_ascii=False, indent=2)
    )
    await session.commit()
    await publish_task_event(task, event)
    await enqueue_task(task.id)
    return {"state": state, "task_status": task.status.value, "error": None}
