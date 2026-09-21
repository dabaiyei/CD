"""Persisted image dependencies for video prompts; waiting releases the worker slot."""
from __future__ import annotations

import base64
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.db.models import AIModel, AITask, Asset, Chapter, Project, Provider, StoryboardShot, StoryboardVersion, TaskStatus, User
from app.services.creation_context import contains_combat
from app.services.billing import resolve_task_pricing
from app.services.image_model_routing import resolve_image_model
from app.services.task_submission import active_tasks, create_queued_task


def _portrait_asset_id(url: str | None) -> str:
    """Asset id embedded in an asset image key, or "" for a designed first frame.

    Asset images are stored as `<tenant>/projects/<project>/assets/<asset-id>-<random>.webp`,
    so the id survives asset regeneration while the file name does not. Ids are
    UUID4 text, hence the fixed 36-character prefix.
    """
    if not url or "/assets/" not in url:
        return ""
    name = url.rsplit("/", 1)[-1]
    if not name.endswith(".webp") or len(name) < 37 or name[36] != "-":
        return ""
    return name[:36]


def _is_asset_portrait(url: str | None, assets) -> bool:
    """Whether this reference is an asset's own image rather than a designed frame.

    Two signals, because either can be the only one available:
    - the id embedded in the key, which survives asset regeneration (the file
      name changes, so comparing URLs alone would miss it);
    - the exact media URL, for keys that do not follow the asset layout
      (global assets, and records created before names were standardized).
    """
    if not url:
        return False
    if any(url == asset.media_url for asset in assets):
        return True
    return bool(_portrait_asset_id(url)) and any(
        _portrait_asset_id(url) == asset.id for asset in assets
    )


def needs_combat_frame(shot, assets) -> bool:
    combat = contains_combat(" ".join([shot.title, shot.scene_description, shot.action_description]))
    combat = combat or any((a.asset_metadata or {}).get("combat_technique") for a in assets)
    if not combat:
        return False
    # Older releases used a character portrait as the shot's first frame.
    if not shot.reference_image_url:
        return True
    return _is_asset_portrait(shot.reference_image_url, assets)


async def wake_ready_parents(session):
    parents = (await session.scalars(select(AITask).where(
        AITask.status == TaskStatus.QUEUED,
        AITask.request_payload["first_frame_waiting"].as_boolean().is_(True),
    ).with_for_update(skip_locked=True))).all()
    for parent in parents:
        ids = parent.request_payload.get("first_frame_task_ids", [])
        states = (await session.scalars(select(AITask.status).where(AITask.id.in_(ids)))).all()
        if not any(s in {TaskStatus.QUEUED, TaskStatus.RUNNING} for s in states):
            parent.request_payload = {**parent.request_payload, "first_frame_waiting": False}


async def prepare(task_id: str) -> bool:
    """Return False after committing children and parking the parent until they finish."""
    from app.services import task_worker as w
    from app.services.task_queue import enqueue_task
    queued = []
    async with w.SessionLocal() as session:
        task = await w.owned_task_for_update(session, task_id)
        if not w.owns_running_task(task):
            return False
        project = await session.get(Project, task.project_id)
        if project is None or project.owner_id != task.user_id:
            raise RuntimeError("首帧所需项目不可用")
        shots = (await session.scalars(select(StoryboardShot).where(
            StoryboardShot.id.in_(task.request_payload.get("shot_ids", [])),
            StoryboardShot.project_id == task.project_id, StoryboardShot.user_id == task.user_id,
        ))).all()
        missing = []
        for shot in shots:
            board = await session.get(StoryboardVersion, shot.storyboard_version_id)
            if not board or not board.is_active or board.id != task.request_payload.get("storyboard_version_id"):
                raise RuntimeError("首帧任务所用分镜版本已失效")
            if shot.version != task.request_payload.get("shot_versions", {}).get(shot.id):
                raise RuntimeError("镜头已编辑，请重新生成提示词")
            assets = (await session.scalars(select(Asset).where(Asset.id.in_(shot.asset_ids), Asset.user_id == task.user_id))).all()
            needs_frame = needs_combat_frame(shot, assets)
            layout = next((row.get("frame_layout") for row in (board.content or [])
                if row.get("order_index") == shot.order_index), None)
            if layout and not _is_asset_portrait(shot.reference_image_url, assets):
                needs_frame = True
            if shot.reference_image_url and not needs_frame:
                generated = await session.scalar(select(AITask).where(
                    AITask.project_id == task.project_id, AITask.user_id == task.user_id,
                    AITask.task_type == "shot_first_frame_generation", AITask.status == TaskStatus.SUCCEEDED,
                    AITask.request_payload["shot_id"].as_string() == shot.id,
                    AITask.result_payload["media_url"].as_string() == shot.reference_image_url,
                ).order_by(AITask.created_at.desc()).limit(1))
                if generated and (generated.result_payload or {}).get("spatial_prompt_version", 0) < 2:
                    needs_frame = True
            if not needs_frame and shot.reference_image_url:
                key = w.object_key_from_media_url(shot.reference_image_url)
                if key:
                    try:
                        await w.materialize_media_file(key)
                    except (FileNotFoundError, ValueError):
                        needs_frame = contains_combat(shot.action_description + shot.title) or any((a.asset_metadata or {}).get("combat_technique") for a in assets)
            if needs_frame:
                missing.append(shot)
        if not missing:
            return True
        capabilities = task.request_payload.get("target_video_model", {}).get("capabilities", {})
        image_limit = capabilities.get("reference_limits", {}).get("image", {})
        if not image_limit.get("enabled") or int(image_limit.get("max_count") or 0) < 1:
            raise RuntimeError("当前视频模型不支持图片参考，无法执行战斗首帧约束，请选择支持图生视频的模型")
        if not set(capabilities.get("generation_modes") or []).intersection({"first_frame", "multi_shot"}):
            raise RuntimeError("当前视频模型没有可执行的首帧或多图参考模式，无法保持战斗首帧，请更换模型")
        pending = await active_tasks(session, project_id=project.id, task_type="shot_first_frame_generation")
        model = await resolve_image_model(session, tenant_id=task.tenant_id, resolution=project.image_resolution,
            fallback_model_id=project.image_model_id)
        if model is None:
            raise RuntimeError("未配置可用的项目首帧图片模型")
        user = await session.get(User, task.user_id)
        pricing = await resolve_task_pricing(session, tenant_id=task.tenant_id, task_type="asset_image_generation")
        ids = []
        attempts = dict(task.request_payload.get("first_frame_attempts", {}))
        for shot in missing:
            existing = next((p for p in pending if p.request_payload.get("shot_id") == shot.id and p.request_payload.get("shot_version") == shot.version), None)
            if existing:
                ids.append(existing.id)
                continue
            if attempts.get(shot.id, 0) >= 3:
                raise RuntimeError(f"镜头 {shot.order_index} 首帧连续生成失败，请检查图片模型后重试")
            child, event = await create_queued_task(session, user=user, project_id=project.id,
                task_type="shot_first_frame_generation", model_id=model.id, cost=pricing.total_cost,
                request_payload={"shot_id": shot.id, "shot_version": shot.version,
                    "chapter_id": shot.chapter_id, "storyboard_version_id": shot.storyboard_version_id,
                    "parent_task_id": task.id, "pricing": pricing.as_payload()},
                message=f"镜头 {shot.order_index} 战斗首帧生成")
            attempts[shot.id] = attempts.get(shot.id, 0) + 1
            ids.append(child.id)
            queued.append((child, event))
        task.request_payload = {**task.request_payload, "first_frame_waiting": True,
            "first_frame_task_ids": ids, "first_frame_attempts": attempts}
        task.status = TaskStatus.QUEUED
        task.worker_id = None
        task.lease_expires_at = None
        event = w.record_task_event(session, task, status=TaskStatus.QUEUED, progress=5,
            message=f"正在准备 {len(ids)} 个战斗首帧，完成后自动继续视频提示词")
        await session.commit()
        await w.publish_task_event(task, event)
        for child, child_event in queued:
            await enqueue_task(child.id)
            await w.publish_task_event(child, child_event)
    return False


async def execute(task_id, gateway_factory):
    # Legacy queued tasks must not overwrite a shot after independent frames were retired.
    raise RuntimeError("独立首帧任务已停用；请继续视频提示词任务，同场视频将使用前镜真实尾帧")


async def _legacy_execute(task_id, gateway_factory):
    from app.services import task_worker as w
    async with w.SessionLocal() as session:
        task = await w.owned_task_for_update(session, task_id)
        if not w.owns_running_task(task):
            return
        shot = await session.get(StoryboardShot, task.request_payload["shot_id"])
        project = await session.get(Project, task.project_id)
        model = await session.get(AIModel, task.model_id)
        if not shot or not project or shot.user_id != task.user_id or shot.project_id != project.id or shot.version != task.request_payload["shot_version"]:
            raise RuntimeError("首帧镜头已修改或不可用")
        board = await session.get(StoryboardVersion, shot.storyboard_version_id)
        chapter = await session.get(Chapter, shot.chapter_id)
        parent = await session.get(AITask, task.request_payload["parent_task_id"])
        if not board or not board.is_active or not chapter or chapter.active_script_version_id != board.script_version_id or not parent or parent.status in {TaskStatus.CANCELLED, TaskStatus.FAILED}:
            raise RuntimeError("首帧所属流程已取消或分镜失效")
        if not model or not model.enabled or model.tenant_id != task.tenant_id:
            raise RuntimeError("首帧图片模型不可用")
        provider = await session.get(Provider, model.provider_id)
        if not provider or not provider.enabled or provider.tenant_id != task.tenant_id:
            raise RuntimeError("首帧图片平台不可用")
        assets = (await session.scalars(select(Asset).where(Asset.id.in_(shot.asset_ids),
            Asset.project_id == project.id, Asset.user_id == task.user_id))).all()
        if len(assets) != len(set(shot.asset_ids)):
            raise RuntimeError("镜头引用了不可用资产")
        assets_by_id = {a.id: a for a in assets}
        assets = [assets_by_id[identity] for identity in dict.fromkeys(shot.asset_ids)]
        parent_ids = {a.parent_asset_id for a in assets if (a.asset_metadata or {}).get("combat_technique") and a.parent_asset_id} - {a.id for a in assets}
        if parent_ids:
            parents = (await session.scalars(select(Asset).where(Asset.id.in_(parent_ids),
                Asset.project_id == project.id, Asset.user_id == task.user_id))).all()
            if len(parents) != len(parent_ids):
                raise RuntimeError("招式所属人物已删除，无法保持首帧身份")
            assets = [*parents, *assets]
        references = []
        for asset in assets:
            key = w.object_key_from_media_url(asset.media_url)
            if not key:
                raise RuntimeError(f"请先生成资产参考图：{asset.name}")
            data = await w.object_storage().get_bytes(key)
            mime = w.media_content_type_from_key_or_bytes(key, data)
            references.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
        from app.services.frame_composition import build_frame_prompt
        layout = next((row.get("frame_layout") for row in (board.content or [])
            if row.get("order_index") == shot.order_index), None)
        prompt = build_frame_prompt(shot, assets, layout)
        request = w.ImageGenerationRequest(model=model.model_id, prompt=prompt,
            resolution=project.image_resolution, aspect_ratio=project.aspect_ratio,
            capabilities=model.capabilities, idempotency_key=task.idempotency_key or task.id,
            reference_image_url=references[0] if references else None, reference_image_urls=references,
            generation_mode="image_to_image" if references else "text_to_image")
        gateway = gateway_factory(provider)
        shot_id, shot_version = shot.id, shot.version
        tenant_id, project_id = task.tenant_id, project.id
        asset_versions = {a.id: a.version for a in assets}
    await w.record_progress(task_id, 25, "正在结合人物、招式与场景参考图生成战斗首帧")
    data = await gateway.generate_image(request)
    path = await run_in_threadpool(w.save_agent_chat_image, data,
        uploads_root=w.get_settings().uploads_root, tenant_id=tenant_id, project_id=project_id)
    try:
        key, url = await w.persist_media_file(path, "image/webp")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    try:
        async with w.SessionLocal() as session:
            task = await w.owned_task_for_update(session, task_id)
            shot = await session.scalar(select(StoryboardShot).where(StoryboardShot.id == shot_id).with_for_update())
            board = await session.get(StoryboardVersion, shot.storyboard_version_id) if shot else None
            parent = await session.get(AITask, task.request_payload["parent_task_id"]) if task else None
            if not w.owns_running_task(task) or not shot or shot.version != shot_version or not board or not board.is_active or not parent or parent.status in {TaskStatus.CANCELLED, TaskStatus.FAILED}:
                raise RuntimeError("镜头已修改，首帧没有覆盖新版本")
            current_assets = (await session.scalars(select(Asset).where(Asset.id.in_(asset_versions)).with_for_update())).all()
            if {a.id: a.version for a in current_assets} != asset_versions:
                raise RuntimeError("人物或招式参考已修改，请重新生成首帧")
            shot.reference_image_url = url
            shot.video_prompt = ""
            task.status = TaskStatus.SUCCEEDED
            task.result_payload = {"shot_id": shot.id, "media_url": url, "source_asset_versions": asset_versions,
                "frame_layout": layout, "effective_image_prompt": prompt, "spatial_prompt_version": 2}
            event = w.record_task_event(session, task, status=TaskStatus.SUCCEEDED, progress=100,
                message=f"镜头 {shot.order_index} 首帧已保存")
            await session.commit()
    except Exception:
        await w.cleanup_media(key, path)
        raise
    await w.publish_task_event(task, event)
