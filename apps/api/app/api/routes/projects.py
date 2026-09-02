from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.models import (
    AgentChatSession,
    AIModel,
    AITask,
    Handbook,
    HandbookType,
    ModelType,
    Project,
    TaskStatus,
    User,
    UserRole,
)
from app.db.session import get_session
from app.domain.schemas import (
    CoverGenerationRequest,
    HandbookPublic,
    ModelPublic,
    ProjectCreate,
    ProjectOptions,
    ProjectPublic,
    ProjectUpdate,
    TaskPublic,
)
from app.services.agent_runtime import AgentRuntimeClient
from app.services.billing import resolve_task_pricing
from app.services.media import (
    ALLOWED_COVER_TYPES,
    MAX_COVER_BYTES,
    InvalidCoverImage,
    save_project_cover,
)
from app.services.object_storage import delete_media_file, delete_media_prefix, persist_media_file
from app.services.readiness import require_core_models_ready
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import active_tasks, create_queued_task

router = APIRouter(prefix="/projects", tags=["projects"])
settings = get_settings()
logger = logging.getLogger(__name__)
REQUIRED_PROJECT_RELATIONS = {
    "video_model_id",
    "image_model_id",
    "visual_handbook_id",
    "director_handbook_id",
}


async def project_for_user(session: AsyncSession, project_id: str, user: User) -> Project:
    project = await session.get(Project, project_id)
    if project is None or project.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    if user.role != UserRole.ADMIN and project.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问此项目")
    return project


async def validate_project_relations(
    session: AsyncSession, tenant_id: str, values: dict[str, object]
) -> None:
    checks = [
        ("video_model_id", AIModel, ModelType.VIDEO),
        ("image_model_id", AIModel, ModelType.IMAGE),
        ("visual_handbook_id", Handbook, HandbookType.VISUAL),
        ("director_handbook_id", Handbook, HandbookType.DIRECTOR),
    ]
    for field, entity, expected_type in checks:
        value = values.get(field)
        if field in values and value is None and field in REQUIRED_PROJECT_RELATIONS:
            raise HTTPException(status_code=422, detail=f"{field} 是项目必填配置")
        if value is None:
            continue
        record = await session.get(entity, value)
        actual_type = (
            record.model_type if isinstance(record, AIModel) else record.handbook_type if record else None
        )
        if (
            record is None
            or record.tenant_id != tenant_id
            or actual_type != expected_type
            or not record.enabled
        ):
            raise HTTPException(
                status_code=422, detail=f"{field} 不是当前租户可用的{expected_type.value}配置"
            )


@router.get("", response_model=list[ProjectPublic])
async def list_projects(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Project]:
    query = select(Project).where(Project.tenant_id == user.tenant_id)
    if user.role != UserRole.ADMIN:
        query = query.where(Project.owner_id == user.id)
    return list((await session.scalars(query.order_by(Project.updated_at.desc()))).all())


@router.post("", response_model=ProjectPublic, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Project:
    values = payload.model_dump()
    await require_core_models_ready(session, user.tenant_id)
    await validate_project_relations(session, user.tenant_id, values)
    project = Project(tenant_id=user.tenant_id, owner_id=user.id, **values)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.get("/options", response_model=ProjectOptions)
async def project_options(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectOptions:
    models = list(
        (
            await session.scalars(
                select(AIModel).where(AIModel.tenant_id == user.tenant_id, AIModel.enabled.is_(True))
            )
        ).all()
    )
    handbooks = list(
        (
            await session.scalars(
                select(Handbook).where(Handbook.tenant_id == user.tenant_id, Handbook.enabled.is_(True))
            )
        ).all()
    )
    return ProjectOptions(
        video_models=[
            ModelPublic.model_validate(item) for item in models if item.model_type == ModelType.VIDEO
        ],
        image_models=[
            ModelPublic.model_validate(item) for item in models if item.model_type == ModelType.IMAGE
        ],
        visual_handbooks=[
            HandbookPublic.model_validate(item)
            for item in handbooks
            if item.handbook_type == HandbookType.VISUAL
        ],
        director_handbooks=[
            HandbookPublic.model_validate(item)
            for item in handbooks
            if item.handbook_type == HandbookType.DIRECTOR
        ],
    )


@router.get("/{project_id}", response_model=ProjectPublic)
async def get_project(
    project_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Project:
    return await project_for_user(session, project_id, user)


@router.patch("/{project_id}", response_model=ProjectPublic)
async def update_project(
    project_id: str,
    payload: ProjectUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Project:
    project = await project_for_user(session, project_id, user)
    values = payload.model_dump(exclude_unset=True)
    await validate_project_relations(session, user.tenant_id, values)
    for field, value in values.items():
        setattr(project, field, value)
    await session.commit()
    await session.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    project = await project_for_user(session, project_id, user)
    active_task_id = await session.scalar(
        select(AITask.id)
        .where(
            AITask.project_id == project.id,
            AITask.status.in_((TaskStatus.QUEUED, TaskStatus.RUNNING)),
        )
        .limit(1)
    )
    if active_task_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="项目仍有排队中或进行中的任务，请先停止任务后再删除",
        )

    runtime_session_ids = list(
        (
            await session.scalars(
                select(AgentChatSession.id).where(AgentChatSession.project_id == project.id)
            )
        ).all()
    )
    tenant_id = project.tenant_id
    await session.delete(project)
    await session.commit()

    runtime = AgentRuntimeClient(
        settings.agent_runtime_url,
        settings.agent_runtime_internal_token,
        settings.agent_runtime_timeout_seconds,
    )
    if runtime_session_ids:
        cleanup_results = await asyncio.gather(
            *(
                runtime.delete_session(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    session_id=session_id,
                )
                for session_id in runtime_session_ids
            ),
            return_exceptions=True,
        )
        for session_id, result in zip(runtime_session_ids, cleanup_results, strict=True):
            if isinstance(result, Exception):
                logger.warning(
                    "Failed to delete Agent Runtime session %s for project %s: %s",
                    session_id,
                    project_id,
                    result,
                )
    try:
        await delete_media_prefix(f"{tenant_id}/projects/{project_id}")
    except Exception:
        logger.exception("Failed to delete media prefix for project %s", project_id)


@router.post("/{project_id}/cover/upload", response_model=ProjectPublic)
async def upload_project_cover(
    project_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Project:
    project = await project_for_user(session, project_id, user)
    if file.content_type not in ALLOWED_COVER_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 JPG、PNG 或 WebP 图片")

    data = await file.read(MAX_COVER_BYTES + 1)
    await file.close()
    if len(data) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="封面图片不能超过 8 MB")
    if not data:
        raise HTTPException(status_code=422, detail="上传的图片为空")

    try:
        _local_url, stored_path = await run_in_threadpool(
            save_project_cover,
            data,
            uploads_root=settings.uploads_root,
            tenant_id=user.tenant_id,
            project_id=project.id,
        )
    except InvalidCoverImage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        storage_key, cover_url = await persist_media_file(stored_path, "image/webp")
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise

    project.cover_url = cover_url
    try:
        await session.commit()
    except Exception:
        await delete_media_file(storage_key, stored_path)
        await session.rollback()
        raise
    await session.refresh(project)
    return project


@router.post("/{project_id}/cover/generate", response_model=TaskPublic, status_code=status.HTTP_202_ACCEPTED)
async def generate_project_cover(
    project_id: str,
    payload: CoverGenerationRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    project = await project_for_user(session, project_id, user)
    if await active_tasks(
        session,
        project_id=project.id,
        task_type="project_cover_generation",
    ):
        raise HTTPException(status_code=409, detail="当前项目已有封面生成任务正在处理")
    image_model = await session.get(AIModel, project.image_model_id) if project.image_model_id else None
    if image_model is None:
        image_model = await session.scalar(
            select(AIModel).where(
                AIModel.tenant_id == user.tenant_id,
                AIModel.model_type == ModelType.IMAGE,
                AIModel.is_default.is_(True),
                AIModel.enabled.is_(True),
            )
        )
    if (
        image_model is None
        or image_model.tenant_id != user.tenant_id
        or image_model.model_type != ModelType.IMAGE
        or not image_model.enabled
    ):
        raise HTTPException(status_code=409, detail="当前项目没有可用的图片模型")
    handbook = await session.get(Handbook, project.visual_handbook_id) if project.visual_handbook_id else None
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="project_cover_generation",
    )

    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project.id,
        task_type="project_cover_generation",
        model_id=image_model.id,
        cost=pricing.total_cost,
        request_payload={
            "project_name": project.name,
            "project_description": project.description,
            "visual_handbook_id": project.visual_handbook_id,
            "visual_handbook_version": handbook.version if handbook else None,
            "image_model_id": image_model.model_id,
            "style_hint": payload.style_hint,
            "pricing": pricing.as_payload(),
        },
        message="AI 生成项目封面",
    )
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task
