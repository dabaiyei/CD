"""Persisted derivative dependencies shared by manual and director image tasks."""
from sqlalchemy import select
from app.db.models import AITask, Asset, TaskStatus


async def wake_asset_dependencies(session):
    tasks = (await session.scalars(select(AITask).where(
        AITask.status == TaskStatus.QUEUED,
        AITask.request_payload["asset_parent_waiting"].as_boolean().is_(True),
    ).with_for_update(skip_locked=True))).all()
    for task in tasks:
        asset = await session.get(Asset, task.request_payload.get("asset_id"))
        if asset and (asset.asset_metadata or {}).get("combat_technique"):
            task.request_payload = {**task.request_payload, "asset_parent_waiting": False,
                                    "asset_parent_task_id": None}
            continue
        dependency = await session.get(AITask, task.request_payload.get("asset_parent_task_id"))
        if dependency is None or dependency.status not in {TaskStatus.RUNNING, TaskStatus.QUEUED}:
            task.request_payload = {**task.request_payload, "asset_parent_waiting": False}


async def prepare_asset_parent(session, task, asset):
    from app.services.asset_tasks import asset_has_ready_image
    from app.services.task_events import record_task_event
    if not asset.parent_asset_id:
        return True
    if (asset.asset_metadata or {}).get("combat_technique"):
        parent = await session.get(Asset, asset.parent_asset_id)
        if not parent or parent.project_id != task.project_id or parent.user_id != task.user_id or parent.tenant_id != task.tenant_id:
            raise RuntimeError("招式所属人物不存在或无权访问")
        return True
    previous_dependency_id = task.request_payload.get("asset_parent_task_id")
    if previous_dependency_id:
        previous = await session.get(AITask, previous_dependency_id)
        if previous is None or previous.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            raise RuntimeError("主资产生图失败或已取消，本次衍生生图已停止，请完成主图后重试")
    parent = await session.get(Asset, asset.parent_asset_id)
    if parent is None or parent.project_id != task.project_id or parent.user_id != task.user_id or parent.tenant_id != task.tenant_id:
        raise RuntimeError("衍生资产的主资产不存在或无权访问")
    seen = {asset.id}
    ancestor = parent
    while ancestor:
        if ancestor.id in seen:
            raise RuntimeError("资产父子关系存在循环，无法生成")
        seen.add(ancestor.id)
        ancestor = await session.get(Asset, ancestor.parent_asset_id) if ancestor.parent_asset_id else None
    dependency = await session.scalar(select(AITask).where(
        AITask.project_id == task.project_id, AITask.user_id == task.user_id,
        AITask.task_type == "asset_image_generation",
        AITask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
        AITask.request_payload["asset_id"].as_string() == parent.id,
    ))
    if dependency:
        task.request_payload = {**task.request_payload, "asset_parent_waiting": True,
            "asset_parent_task_id": dependency.id}
        task.status = TaskStatus.QUEUED
        task.worker_id = None
        task.lease_expires_at = None
        event = record_task_event(session, task, status=TaskStatus.QUEUED, progress=5,
            message=f"等待主资产“{parent.name}”图片，完成后自动生成衍生资产")
        await session.commit()
        from app.services.task_events import publish_task_event
        await publish_task_event(task, event)
        return False
    if not await asset_has_ready_image(parent):
        raise RuntimeError(f"主资产“{parent.name}”尚无可用图片，请先完成主资产生图后重试")
    return True
