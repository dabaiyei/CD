from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import User, UserSkill, UserSkillStage
from app.db.session import get_session
from app.domain.schemas import UserSkillCreate, UserSkillPublic, UserSkillUpdate
from app.services.user_skills import USER_SKILL_STAGE_LABELS

router = APIRouter(prefix="/user-skills", tags=["user-skills"])


async def owned_user_skill(
    session: AsyncSession,
    *,
    skill_id: str,
    user: User,
) -> UserSkill:
    skill = await session.get(UserSkill, skill_id)
    if skill is None or skill.tenant_id != user.tenant_id or skill.user_id != user.id:
        raise HTTPException(status_code=404, detail="个人 Skill 不存在")
    return skill


async def ensure_unique_name(
    session: AsyncSession,
    *,
    user: User,
    name: str,
    exclude_id: str | None = None,
) -> None:
    conditions = [
        UserSkill.tenant_id == user.tenant_id,
        UserSkill.user_id == user.id,
        func.lower(UserSkill.name) == name.casefold(),
    ]
    if exclude_id:
        conditions.append(UserSkill.id != exclude_id)
    duplicate = await session.scalar(select(UserSkill.id).where(*conditions))
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="已有同名个人 Skill")


@router.get("/stages")
async def list_user_skill_stages(
    _user: User = Depends(get_current_user),
) -> list[dict[str, str]]:
    return [
        {"value": stage.value, "label": USER_SKILL_STAGE_LABELS[stage]}
        for stage in UserSkillStage
    ]


@router.get("", response_model=list[UserSkillPublic])
async def list_user_skills(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[UserSkill]:
    return list(
        (
            await session.scalars(
                select(UserSkill)
                .where(
                    UserSkill.tenant_id == user.tenant_id,
                    UserSkill.user_id == user.id,
                )
                .order_by(UserSkill.updated_at.desc(), UserSkill.id.desc())
            )
        ).all()
    )


@router.post("", response_model=UserSkillPublic, status_code=status.HTTP_201_CREATED)
async def create_user_skill(
    payload: UserSkillCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserSkill:
    await ensure_unique_name(session, user=user, name=payload.name)
    skill = UserSkill(
        tenant_id=user.tenant_id,
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        trigger_stages=[stage.value for stage in payload.trigger_stages],
        enabled=payload.enabled,
    )
    session.add(skill)
    await session.commit()
    await session.refresh(skill)
    return skill


@router.patch("/{skill_id}", response_model=UserSkillPublic)
async def update_user_skill(
    skill_id: str,
    payload: UserSkillUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserSkill:
    skill = await owned_user_skill(session, skill_id=skill_id, user=user)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        await ensure_unique_name(
            session,
            user=user,
            name=changes["name"],
            exclude_id=skill.id,
        )
        skill.name = changes["name"]
    if "description" in changes:
        skill.description = changes["description"]
    if "trigger_stages" in changes:
        skill.trigger_stages = [stage.value for stage in changes["trigger_stages"]]
    if "enabled" in changes:
        skill.enabled = changes["enabled"]
    if changes:
        skill.version += 1
    await session.commit()
    await session.refresh(skill)
    return skill


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_skill(
    skill_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    skill = await owned_user_skill(session, skill_id=skill_id, user=user)
    await session.delete(skill)
    await session.commit()
