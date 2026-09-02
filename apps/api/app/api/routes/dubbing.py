from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.agent_chat import resolve_text_model
from app.api.routes.director import chapter_for_user
from app.api.routes.projects import project_for_user
from app.db.models import (
    AgentKind,
    AgentProfile,
    AIModel,
    AITask,
    Asset,
    AssetScope,
    AssetType,
    AudioClip,
    AudioClipStatus,
    DialogueLine,
    DialogueVersion,
    ModelType,
    ScriptVersion,
    StoryboardVersion,
    User,
    VoiceBinding,
)
from app.db.session import get_session
from app.domain.schemas import (
    DialogueBatchGenerationRequest,
    DialogueLinePublic,
    DialogueLineUpdate,
    DialogueVersionDetail,
    DialogueVersionPublic,
    DubbingOptions,
    TaskPublic,
    VoiceBindingPublic,
    VoiceBindingUpsert,
)
from app.services.billing import ResolvedTaskPricing, resolve_task_pricing
from app.services.composition import invalidate_compositions
from app.services.task_events import publish_task_event
from app.services.task_queue import enqueue_task
from app.services.task_submission import active_tasks, create_queued_task

router = APIRouter(prefix="/projects", tags=["dialogue-dubbing"])


async def general_agent(session: AsyncSession, tenant_id: str) -> AgentProfile:
    agent = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.kind == AgentKind.GENERAL,
            AgentProfile.enabled.is_(True),
        )
    )
    if agent is None:
        raise HTTPException(status_code=409, detail="管理员尚未配置可用的通用 AI")
    return agent


async def dialogue_for_user(
    session: AsyncSession,
    *,
    project_id: str,
    chapter_id: str,
    dialogue_id: str,
    user: User,
) -> tuple[DialogueVersion, object]:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    dialogue = await session.get(DialogueVersion, dialogue_id)
    if dialogue is None or dialogue.chapter_id != chapter.id or dialogue.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="台词版本不存在")
    return dialogue, chapter


@router.get("/{project_id}/dubbing/options", response_model=DubbingOptions)
async def dubbing_options(
    project_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DubbingOptions:
    await project_for_user(session, project_id, user)
    models = list(
        (
            await session.scalars(
                select(AIModel)
                .where(
                    AIModel.tenant_id == user.tenant_id,
                    AIModel.model_type == ModelType.TTS,
                    AIModel.enabled.is_(True),
                )
                .order_by(AIModel.is_default.desc(), AIModel.name)
            )
        ).all()
    )
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.tenant_id == user.tenant_id,
                    Asset.project_id == project_id,
                    Asset.scope == AssetScope.PROJECT,
                    Asset.asset_type == AssetType.CHARACTER,
                )
                .order_by(Asset.name)
            )
        ).all()
    )
    bindings = list(
        (
            await session.scalars(
                select(VoiceBinding)
                .where(
                    VoiceBinding.tenant_id == user.tenant_id,
                    VoiceBinding.project_id == project_id,
                )
                .order_by(VoiceBinding.updated_at.desc())
            )
        ).all()
    )
    return DubbingOptions(tts_models=models, character_assets=assets, voice_bindings=bindings)


@router.put("/{project_id}/dubbing/voice-bindings", response_model=VoiceBindingPublic)
async def upsert_voice_binding(
    project_id: str,
    payload: VoiceBindingUpsert,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> VoiceBinding:
    await project_for_user(session, project_id, user)
    asset = await session.get(Asset, payload.character_asset_id)
    model = await session.get(AIModel, payload.tts_model_id)
    if (
        asset is None
        or asset.tenant_id != user.tenant_id
        or asset.project_id != project_id
        or asset.scope != AssetScope.PROJECT
        or asset.asset_type != AssetType.CHARACTER
    ):
        raise HTTPException(status_code=422, detail="只能为当前项目的人物资产绑定音色")
    if (
        model is None
        or model.tenant_id != user.tenant_id
        or model.model_type != ModelType.TTS
        or not model.enabled
    ):
        raise HTTPException(status_code=409, detail="所选 TTS 模型不可用")
    binding = await session.scalar(
        select(VoiceBinding).where(
            VoiceBinding.project_id == project_id,
            VoiceBinding.character_asset_id == asset.id,
        )
    )
    values = payload.model_dump(exclude={"character_asset_id"})
    if binding is None:
        binding = VoiceBinding(
            tenant_id=user.tenant_id,
            user_id=user.id,
            project_id=project_id,
            character_asset_id=asset.id,
            **values,
        )
        session.add(binding)
    else:
        binding.version += 1
        for field, value in values.items():
            setattr(binding, field, value)
        await session.execute(
            update(AudioClip)
            .where(AudioClip.voice_binding_id == binding.id, AudioClip.is_active.is_(True))
            .values(is_active=False, invalidated_reason=f"角色音色已更新为 v{binding.version}")
        )
        chapter_ids = list(
            (
                await session.scalars(
                    select(AudioClip.chapter_id).where(AudioClip.voice_binding_id == binding.id).distinct()
                )
            ).all()
        )
        for chapter_id in chapter_ids:
            await invalidate_compositions(
                session,
                chapter_id=chapter_id,
                reason=f"角色音色已更新为 v{binding.version}",
            )
    await session.commit()
    await session.refresh(binding)
    return binding


@router.delete("/{project_id}/dubbing/voice-bindings/{binding_id}", status_code=204)
async def delete_voice_binding(
    project_id: str,
    binding_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await project_for_user(session, project_id, user)
    binding = await session.get(VoiceBinding, binding_id)
    if binding is None or binding.project_id != project_id or binding.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="音色绑定不存在")
    clip_count = await session.scalar(
        select(func.count(AudioClip.id)).where(AudioClip.voice_binding_id == binding.id)
    )
    if clip_count:
        binding.enabled = False
        binding.version += 1
        await session.execute(
            update(AudioClip)
            .where(AudioClip.voice_binding_id == binding.id, AudioClip.is_active.is_(True))
            .values(is_active=False, invalidated_reason="角色音色绑定已停用")
        )
        chapter_ids = list(
            (
                await session.scalars(
                    select(AudioClip.chapter_id).where(AudioClip.voice_binding_id == binding.id).distinct()
                )
            ).all()
        )
        for chapter_id in chapter_ids:
            await invalidate_compositions(
                session,
                chapter_id=chapter_id,
                reason="角色音色绑定已停用",
            )
    else:
        await session.delete(binding)
    await session.commit()


@router.get(
    "/{project_id}/chapters/{chapter_id}/dialogues",
    response_model=list[DialogueVersionPublic],
)
async def list_dialogue_versions(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DialogueVersion]:
    await chapter_for_user(session, project_id, chapter_id, user)
    return list(
        (
            await session.scalars(
                select(DialogueVersion)
                .where(DialogueVersion.chapter_id == chapter_id)
                .order_by(DialogueVersion.version.desc())
            )
        ).all()
    )


@router.get(
    "/{project_id}/chapters/{chapter_id}/dialogues/{dialogue_id}",
    response_model=DialogueVersionDetail,
)
async def get_dialogue_version(
    project_id: str,
    chapter_id: str,
    dialogue_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DialogueVersionDetail:
    dialogue, _chapter = await dialogue_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        dialogue_id=dialogue_id,
        user=user,
    )
    lines = list(
        (
            await session.scalars(
                select(DialogueLine)
                .where(DialogueLine.dialogue_version_id == dialogue.id)
                .order_by(DialogueLine.order_index)
            )
        ).all()
    )
    clips = list(
        (
            await session.scalars(
                select(AudioClip)
                .where(AudioClip.dialogue_version_id == dialogue.id)
                .order_by(AudioClip.dialogue_line_id, AudioClip.version.desc())
            )
        ).all()
    )
    return DialogueVersionDetail(version=dialogue, lines=lines, audio_clips=clips)


@router.post(
    "/{project_id}/chapters/{chapter_id}/dialogues/generate",
    response_model=TaskPublic,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_dialogues(
    project_id: str,
    chapter_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AITask:
    chapter = await chapter_for_user(session, project_id, chapter_id, user)
    if not chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="请先选择生效剧本")
    script = await session.get(ScriptVersion, chapter.active_script_version_id)
    if script is None:
        raise HTTPException(status_code=409, detail="生效剧本已不存在")
    storyboard = await session.scalar(
        select(StoryboardVersion).where(
            StoryboardVersion.chapter_id == chapter.id,
            StoryboardVersion.is_active.is_(True),
        )
    )
    for pending in await active_tasks(
        session,
        project_id=project_id,
        task_type="chapter_dialogue_extraction",
    ):
        if pending.request_payload.get("chapter_id") == chapter.id:
            raise HTTPException(status_code=409, detail="该章节已有台词提取任务正在处理")
    agent = await general_agent(session, user.tenant_id)
    model, _provider, _api_key = await resolve_text_model(session, agent, user.tenant_id)
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="chapter_dialogue_extraction",
    )
    task, event = await create_queued_task(
        session,
        user=user,
        project_id=project_id,
        task_type="chapter_dialogue_extraction",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "chapter_id": chapter.id,
            "script_version_id": script.id,
            "script_version": script.version,
            "script_hash": hashlib.sha256(script.content.encode("utf-8")).hexdigest(),
            "storyboard_version_id": storyboard.id if storyboard else None,
            "storyboard_version": storyboard.version if storyboard else None,
            "agent_profile_id": agent.id,
            "pricing": pricing.as_payload(),
        },
        message="AI 台词提取",
    )
    await session.commit()
    await session.refresh(task)
    await enqueue_task(task.id)
    await publish_task_event(task, event)
    return task


@router.post(
    "/{project_id}/chapters/{chapter_id}/dialogues/{dialogue_id}/activate",
    response_model=DialogueVersionPublic,
)
async def activate_dialogue_version(
    project_id: str,
    chapter_id: str,
    dialogue_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DialogueVersion:
    dialogue, chapter = await dialogue_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        dialogue_id=dialogue_id,
        user=user,
    )
    if dialogue.script_version_id != chapter.active_script_version_id:
        raise HTTPException(status_code=409, detail="只能启用当前生效剧本生成的台词版本")
    reason = f"已切换为台词 v{dialogue.version}"
    await invalidate_compositions(session, chapter_id=chapter.id, reason=reason)
    await session.execute(
        update(DialogueVersion).where(DialogueVersion.chapter_id == chapter.id).values(is_active=False)
    )
    await session.execute(
        update(AudioClip)
        .where(
            AudioClip.chapter_id == chapter.id,
            AudioClip.dialogue_version_id != dialogue.id,
            AudioClip.is_active.is_(True),
        )
        .values(is_active=False, invalidated_reason=reason)
    )
    dialogue.is_active = True
    dialogue.invalidated_reason = None
    await session.commit()
    await session.refresh(dialogue)
    return dialogue


@router.patch(
    "/{project_id}/chapters/{chapter_id}/dialogues/{dialogue_id}/lines/{line_id}",
    response_model=DialogueLinePublic,
)
async def update_dialogue_line(
    project_id: str,
    chapter_id: str,
    dialogue_id: str,
    line_id: str,
    payload: DialogueLineUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DialogueLine:
    dialogue, _chapter = await dialogue_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        dialogue_id=dialogue_id,
        user=user,
    )
    if not dialogue.is_active:
        raise HTTPException(status_code=409, detail="只能编辑当前生效台词版本")
    line = await session.get(DialogueLine, line_id)
    if line is None or line.dialogue_version_id != dialogue.id:
        raise HTTPException(status_code=404, detail="台词行不存在")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(line, field, value)
    line.version += 1
    line.source_hash = hashlib.sha256(
        f"{line.speaker}\n{line.text}\n{line.emotion}\n{line.direction}".encode()
    ).hexdigest()
    await invalidate_compositions(
        session,
        chapter_id=line.chapter_id,
        reason=f"台词 {line.order_index:02d} 已更新",
    )
    await session.execute(
        update(AudioClip)
        .where(AudioClip.dialogue_line_id == line.id, AudioClip.is_active.is_(True))
        .values(is_active=False, invalidated_reason=f"台词已更新为 v{line.version}")
    )
    await session.commit()
    await session.refresh(line)
    return line


async def prepare_audio_task(
    session: AsyncSession,
    *,
    user: User,
    project_id: str,
    dialogue: DialogueVersion,
    line: DialogueLine,
    binding: VoiceBinding,
    pricing: ResolvedTaskPricing,
) -> tuple[AITask, object]:
    model = await session.get(AIModel, binding.tts_model_id)
    if (
        model is None
        or model.tenant_id != user.tenant_id
        or model.model_type != ModelType.TTS
        or not model.enabled
    ):
        raise HTTPException(status_code=409, detail=f"“{line.speaker}”绑定的 TTS 模型不可用")
    latest = await session.scalar(
        select(func.max(AudioClip.version)).where(AudioClip.dialogue_line_id == line.id)
    )
    clip = AudioClip(
        tenant_id=user.tenant_id,
        user_id=user.id,
        project_id=project_id,
        chapter_id=line.chapter_id,
        dialogue_version_id=dialogue.id,
        dialogue_line_id=line.id,
        voice_binding_id=binding.id,
        model_id=model.id,
        line_version=line.version,
        binding_version=binding.version,
        version=(latest or 0) + 1,
        status=AudioClipStatus.QUEUED,
    )
    session.add(clip)
    await session.flush()
    return await create_queued_task(
        session,
        user=user,
        project_id=project_id,
        task_type="dialogue_tts_generation",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            "chapter_id": line.chapter_id,
            "dialogue_version_id": dialogue.id,
            "dialogue_line_id": line.id,
            "line_version": line.version,
            "voice_binding_id": binding.id,
            "binding_version": binding.version,
            "audio_clip_id": clip.id,
            "pricing": pricing.as_payload(),
        },
        message=f"{line.speaker} · 台词 {line.order_index:02d} 配音",
    )


@router.post(
    "/{project_id}/chapters/{chapter_id}/dialogues/{dialogue_id}/audio/generate",
    response_model=list[TaskPublic],
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_dialogue_audio(
    project_id: str,
    chapter_id: str,
    dialogue_id: str,
    payload: DialogueBatchGenerationRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AITask]:
    dialogue, _chapter = await dialogue_for_user(
        session,
        project_id=project_id,
        chapter_id=chapter_id,
        dialogue_id=dialogue_id,
        user=user,
    )
    if not dialogue.is_active:
        raise HTTPException(status_code=409, detail="只能为当前生效台词版本生成配音")
    line_query = select(DialogueLine).where(DialogueLine.dialogue_version_id == dialogue.id)
    if payload.dialogue_line_ids is not None:
        line_query = line_query.where(DialogueLine.id.in_(payload.dialogue_line_ids))
    lines = list((await session.scalars(line_query.order_by(DialogueLine.order_index))).all())
    if not lines:
        raise HTTPException(status_code=422, detail="没有可生成的台词")
    if payload.dialogue_line_ids is not None and len(lines) != len(payload.dialogue_line_ids):
        raise HTTPException(status_code=422, detail="部分台词行不属于当前版本")
    active_line_ids = {
        str(task.request_payload.get("dialogue_line_id"))
        for task in await active_tasks(
            session,
            project_id=project_id,
            task_type="dialogue_tts_generation",
        )
    }
    conflicts = [line.order_index for line in lines if line.id in active_line_ids]
    if conflicts:
        raise HTTPException(status_code=409, detail=f"台词 {conflicts[0]:02d} 已有配音任务正在处理")
    assets = list(
        (
            await session.scalars(
                select(Asset).where(
                    Asset.project_id == project_id,
                    Asset.asset_type == AssetType.CHARACTER,
                )
            )
        ).all()
    )
    asset_by_name = {asset.name.strip().casefold(): asset for asset in assets}
    bindings = list(
        (
            await session.scalars(
                select(VoiceBinding).where(
                    VoiceBinding.project_id == project_id,
                    VoiceBinding.enabled.is_(True),
                )
            )
        ).all()
    )
    binding_by_asset = {item.character_asset_id: item for item in bindings}
    missing = sorted(
        {
            line.speaker
            for line in lines
            if not (asset := asset_by_name.get(line.speaker.strip().casefold()))
            or asset.id not in binding_by_asset
        }
    )
    if missing:
        raise HTTPException(status_code=409, detail=f"请先为角色绑定音色：{'、'.join(missing[:8])}")
    queued: list[tuple[AITask, object]] = []
    pricing = await resolve_task_pricing(
        session,
        tenant_id=user.tenant_id,
        task_type="dialogue_tts_generation",
    )
    for line in lines:
        asset = asset_by_name[line.speaker.strip().casefold()]
        queued.append(
            await prepare_audio_task(
                session,
                user=user,
                project_id=project_id,
                dialogue=dialogue,
                line=line,
                binding=binding_by_asset[asset.id],
                pricing=pricing,
            )
        )
    await session.commit()
    for task, event in queued:
        await session.refresh(task)
        await enqueue_task(task.id)
        await publish_task_event(task, event)
    return [task for task, _event in queued]
