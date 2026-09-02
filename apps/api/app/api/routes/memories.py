from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.projects import project_for_user
from app.db.models import AgentMemory, User
from app.db.session import get_session
from app.domain.schemas import AgentMemoryPublic, AgentMemoryUpsert
from app.services.agent_memory import EMBEDDING_MODEL, embed_memory_text

router = APIRouter(prefix="/projects/{project_id}/agent-memories", tags=["agent-memory"])


@router.get("", response_model=list[AgentMemoryPublic])
async def list_agent_memories(
    project_id: str,
    namespace: str | None = Query(default=None, min_length=1, max_length=80),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentMemory]:
    await project_for_user(session, project_id, user)
    query = select(AgentMemory).where(
        AgentMemory.tenant_id == user.tenant_id,
        AgentMemory.user_id == user.id,
        AgentMemory.project_id == project_id,
    )
    if namespace is not None:
        query = query.where(AgentMemory.namespace == namespace)
    return list((await session.scalars(query.order_by(AgentMemory.updated_at.desc()))).all())


@router.put("/{namespace}/{memory_key}", response_model=AgentMemoryPublic)
async def upsert_agent_memory(
    project_id: str,
    namespace: Annotated[
        str,
        Path(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
    memory_key: Annotated[
        str,
        Path(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
    payload: AgentMemoryUpsert,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMemory:
    await project_for_user(session, project_id, user)
    memory = await session.scalar(
        select(AgentMemory).where(
            AgentMemory.tenant_id == user.tenant_id,
            AgentMemory.user_id == user.id,
            AgentMemory.project_id == project_id,
            AgentMemory.namespace == namespace,
            AgentMemory.memory_key == memory_key,
        )
    )
    if memory is None:
        memory = AgentMemory(
            tenant_id=user.tenant_id,
            user_id=user.id,
            project_id=project_id,
            namespace=namespace,
            memory_key=memory_key,
            content=payload.content,
            memory_metadata=payload.metadata,
            embedding=embed_memory_text(payload.content),
            embedding_model=EMBEDDING_MODEL,
            salience=0.7,
            is_automatic=False,
        )
        session.add(memory)
    else:
        memory.content = payload.content
        memory.memory_metadata = payload.metadata
        memory.embedding = embed_memory_text(payload.content)
        memory.embedding_model = EMBEDDING_MODEL
        memory.salience = 0.7
        memory.is_automatic = False
        memory.source_session_id = None
        memory.source_message_id = None
    await session.commit()
    await session.refresh(memory)
    return memory
