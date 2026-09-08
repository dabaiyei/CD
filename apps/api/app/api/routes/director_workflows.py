from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.director import chapter_for_user
from app.db.models import (
    DirectorDecisionRequest,
    DirectorWorkflowRun,
    DirectorWorkflowStage,
    DirectorWorkflowStatus,
    StoryboardShot,
    StoryboardVersion,
    User,
)
from app.db.session import get_session
from app.domain.schemas import (
    DirectorDecisionSubmit,
    DirectorWorkflowDetail,
    DirectorWorkflowStart,
)
from app.services.director_orchestration import (
    start_automatic_workflow_from_progress,
    start_script_workflow,
    start_storyboard_workflow,
    start_storyboard_workflow_for_chapter,
    stop_automatic_workflow,
    submit_decision,
    workflow_detail_rows,
    workflow_for_user,
)

router = APIRouter(prefix="/projects", tags=["director-agent-workflows"])


async def _detail(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> dict:
    children, decision = await workflow_detail_rows(session, workflow)
    return {"workflow": workflow, "child_runs": children, "pending_decision": decision}


async def reconcile_stale_storyboard_workflow(
    session: AsyncSession,
    workflow: DirectorWorkflowRun,
) -> None:
    if (
        workflow.current_task_id
        or workflow.stage != DirectorWorkflowStage.READY_FOR_ASSET_IMAGES
        or workflow.status != DirectorWorkflowStatus.WAITING_USER
        or not workflow.script_version_id
    ):
        return
    storyboard = await session.scalar(
        select(StoryboardVersion)
        .where(
            StoryboardVersion.tenant_id == workflow.tenant_id,
            StoryboardVersion.user_id == workflow.user_id,
            StoryboardVersion.project_id == workflow.project_id,
            StoryboardVersion.chapter_id == workflow.chapter_id,
            StoryboardVersion.script_version_id == workflow.script_version_id,
            StoryboardVersion.is_active.is_(True),
        )
        .order_by(StoryboardVersion.version.desc())
        .limit(1)
    )
    if storyboard is None:
        return
    shot_count = await session.scalar(
        select(func.count()).select_from(StoryboardShot).where(
            StoryboardShot.storyboard_version_id == storyboard.id
        )
    )
    if not shot_count:
        return
    workflow.storyboard_version_id = storyboard.id
    workflow.stage = DirectorWorkflowStage.READY_FOR_VIDEO
    workflow.status = DirectorWorkflowStatus.COMPLETED
    workflow.last_error = None
    workflow.last_message = (
        f"检测到已有生效分镜 v{storyboard.version}（{shot_count} 个镜头），"
        "已同步导演流程状态。"
    )


@router.get(
    "/{project_id}/chapters/{chapter_id}/director-workflow",
    response_model=DirectorWorkflowDetail | None,
)
async def get_latest_workflow(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict | None:
    await chapter_for_user(session, project_id, chapter_id, user)
    workflow = await session.scalar(
        select(DirectorWorkflowRun)
        .where(
            DirectorWorkflowRun.project_id == project_id,
            DirectorWorkflowRun.chapter_id == chapter_id,
            DirectorWorkflowRun.tenant_id == user.tenant_id,
            DirectorWorkflowRun.user_id == user.id,
        )
        .order_by(DirectorWorkflowRun.created_at.desc())
        .limit(1)
    )
    if workflow is None:
        return None
    await reconcile_stale_storyboard_workflow(session, workflow)
    await session.commit()
    await session.refresh(workflow)
    return await _detail(session, workflow)


@router.post(
    "/{project_id}/chapters/{chapter_id}/director-workflow",
    response_model=DirectorWorkflowDetail,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_workflow(
    project_id: str,
    chapter_id: str,
    payload: DirectorWorkflowStart,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    try:
        workflow = await start_script_workflow(
            session,
            chapter=chapter,
            user=user,
            instruction=payload.instruction.strip(),
            chat_session_id=payload.chat_session_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return await _detail(session, workflow)


@router.post(
    "/{project_id}/chapters/{chapter_id}/director-workflow/automatic",
    response_model=DirectorWorkflowDetail,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_automatic_workflow(
    project_id: str,
    chapter_id: str,
    payload: DirectorWorkflowStart,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    try:
        workflow = await start_automatic_workflow_from_progress(
            session,
            chapter=chapter,
            user=user,
            instruction=payload.instruction.strip() or "全自动完成本章 AI 视频制作",
            chat_session_id=payload.chat_session_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return await _detail(session, workflow)


@router.post(
    "/{project_id}/director-workflows/{workflow_id}/stop",
    response_model=DirectorWorkflowDetail,
)
async def stop_workflow_automation(
    project_id: str,
    workflow_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    workflow = await workflow_for_user(
        session,
        workflow_id=workflow_id,
        project_id=project_id,
        user=user,
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="导演流程不存在")
    try:
        await stop_automatic_workflow(session, workflow)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await session.refresh(workflow)
    return await _detail(session, workflow)


@router.post(
    "/{project_id}/chapters/{chapter_id}/director-workflow/storyboard",
    response_model=DirectorWorkflowDetail,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_storyboard_workflow_from_chapter(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    try:
        workflow = await start_storyboard_workflow_for_chapter(
            session,
            chapter=chapter,
            user=user,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return await _detail(session, workflow)


@router.post(
    "/{project_id}/director-workflows/{workflow_id}/storyboard",
    response_model=DirectorWorkflowDetail,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_storyboard_from_workflow(
    project_id: str,
    workflow_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    workflow = await workflow_for_user(
        session,
        workflow_id=workflow_id,
        project_id=project_id,
        user=user,
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="导演流程不存在")
    try:
        await start_storyboard_workflow(session, workflow)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await session.refresh(workflow)
    return await _detail(session, workflow)


@router.post(
    "/{project_id}/director-workflows/{workflow_id}/decisions/{decision_id}",
    response_model=DirectorWorkflowDetail,
    status_code=status.HTTP_202_ACCEPTED,
)
async def decide_workflow_review(
    project_id: str,
    workflow_id: str,
    decision_id: str,
    payload: DirectorDecisionSubmit,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    workflow = await workflow_for_user(
        session,
        workflow_id=workflow_id,
        project_id=project_id,
        user=user,
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="导演流程不存在")
    decision = await session.get(DirectorDecisionRequest, decision_id)
    if (
        decision is None
        or decision.workflow_id != workflow.id
        or decision.tenant_id != user.tenant_id
        or decision.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="审核选择不存在")
    try:
        await submit_decision(
            session,
            workflow,
            decision,
            option=payload.option,
            feedback=payload.feedback,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await session.refresh(workflow)
    return await _detail(session, workflow)
