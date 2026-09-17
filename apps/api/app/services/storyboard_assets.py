"""Storyboard-wide asset reconciliation, with sequential durable AI batches."""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.db.models import Asset, AssetExtractionItem, AssetScope, AssetStatus, AssetType, Project
from app.services.asset_identity import asset_name_key, extraction_asset_catalog, reusable_asset
from app.services.asset_revisions import snapshot_asset_revision

RULES = """分镜后资产统一提取规则（优先于剧本资产提取模板）：
本次输入是连续的一组分镜；必须综合所有镜头统一判断资产，不可逐镜独立命名。
只提取分镜实际需要的持久外观变化与招式特效；情绪、机位、临时姿态、普通光照不另建资产。
复用目录内相同身份及相同造型，名称必须原样使用。不要因别名重复创建。
已有资产与前批结果为权威目录；相同主体同一状态必须复用，新的状态才创建衍生。
衍生 parent_name 指向基础资产，同类型。招式为所属人物 character 衍生资产，需提供 technique。
招式图仅描绘特效、武器、召唤物，不画施术者或对手；人物身份由主资产约束。
基础资产通常已经存在，确有遗漏才补建，绝不把换装人物当作新人物。
返回 assets（本批新增资产，允许空数组）与 shots（每个输入镜头的完整资产名称列表）。
shots 必须逐个覆盖输入的 order_index，只能引用目录、前批或本批定义的名称。
同一镜头引用衍生形态时保留其主资产以明确身份，禁止同时引用同一主体互斥的多个造型。
只返回 JSON，不生成视频提示词。
"""


def batches(rows: list[dict], budget: int = 14000) -> list[list[dict]]:
    result, current, size = [], [], 0
    for row in rows:
        length = len(json.dumps(row, ensure_ascii=False))
        if length > budget:
            raise RuntimeError("单个分镜资产描述超过提取预算，请缩短该镜头的场景与动作描述")
        if current and size + length > budget:
            result.append(current)
            current, size = [], 0
        current.append(row)
        size += length
    if current:
        result.append(current)
    return result


class ShotBinding(BaseModel):
    order_index: int = Field(ge=1)
    asset_names: list[str] = Field(default_factory=list, max_length=100)


async def storyboard_response(task_id, prompt_code, prompt, runtime_factory, *, validator=None,
                              timing_plan=None, durations=None):
    """Keep the exact board across extraction retries; never regenerate batch inputs."""
    from app.services import task_worker as worker
    from types import SimpleNamespace
    key = hashlib.sha256((prompt + json.dumps([timing_plan, durations], sort_keys=True)
                          + "structured-board-v2-clips").encode()).hexdigest()
    state = {}
    async with worker.SessionLocal() as session:
        task = await worker.owned_task_for_update(session, task_id)
        if not worker.owns_running_task(task):
            raise RuntimeError("分镜任务已停止")
        cached = task.request_payload.get("storyboard_draft", {})
        if cached.get("key") == key:
            try:
                if validator:
                    validator(cached["response"])
                return SimpleNamespace(final_response=cached["response"], manifest=cached["manifest"])
            except (ValueError, RuntimeError):
                pass  # Never reuse a draft that violates the source timing contract.
        checkpoint = task.request_payload.get("storyboard_generation", {})
        if checkpoint.get("key") == key:
            state = checkpoint.get("state", {})
    request = await worker.runtime_request(task_id, prompt_code=prompt_code, prompt=prompt)
    if durations is not None:
        from app.services.storyboard_generation import generate

        async def save(value):
            async with worker.SessionLocal() as session:
                task = await worker.owned_task_for_update(session, task_id)
                if not worker.owns_running_task(task):
                    raise RuntimeError("分镜任务已停止")
                task.request_payload = {**task.request_payload, "storyboard_generation": {
                    "key": key, "state": json.loads(json.dumps(value))}}
                await session.commit()

        async def progress(message):
            await worker.record_progress(task_id, 35, message)

        text, manifest = await generate(request, runtime_factory, plan=timing_plan or [],
            durations=durations, state=state, save=save, progress=progress)
        if validator:
            validator(text)
        result = SimpleNamespace(final_response=text, manifest=manifest)
    elif validator:
        from app.services.source_timeline import run_validated
        result = await run_validated(request, runtime_factory, validator)
    else:
        result = await runtime_factory().run(request)
    worker.StoryboardGenerationPayload.model_validate(worker.parse_json_object(result.final_response))
    async with worker.SessionLocal() as session:
        task = await worker.owned_task_for_update(session, task_id)
        if not worker.owns_running_task(task):
            raise RuntimeError("分镜任务已停止")
        task.request_payload = {**task.request_payload, "storyboard_draft": {
            "key": key, "response": result.final_response, "manifest": result.manifest}}
        await session.commit()
    return result


async def plan(task_id, shots, runtime_factory):
    from app.services import task_worker as worker
    from app.services.combat_techniques import CombatTechnique

    class Extraction(BaseModel):
        assets: list[worker.ExtractedAssetPayload] = Field(default_factory=list, max_length=500)
        shots: list[ShotBinding]

    rows = [{"order_index": i, **shot.model_dump(mode="json", exclude={"video_prompt"})}
            for i, shot in enumerate(shots, 1)]
    fingerprint = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
    async with worker.SessionLocal() as session:
        task = await worker.owned_task_for_update(session, task_id)
        if not worker.owns_running_task(task):
            raise RuntimeError("分镜资产提取任务已停止")
        catalog = await extraction_asset_catalog(session, task.project_id, task.tenant_id, task.user_id)
        names = {a.id: a.name for a in catalog}
        known = [{"asset_type": a.asset_type.value, "name": a.name,
                  "parent_name": names.get(a.parent_asset_id), "description": a.description[:160]}
                 for a in catalog]
        cached = (task.request_payload or {}).get("storyboard_asset_batches", {})
        completed = cached.get("completed", []) if cached.get("fingerprint") == fingerprint else []
        if cached.get("fingerprint") == fingerprint and cached.get("catalog") is not None:
            known = cached["catalog"]
    schema = json.dumps(CombatTechnique.model_json_schema(), ensure_ascii=False)
    outputs = []
    offset = 0
    while offset < len(rows):
        index = len(outputs)
        prior = [{k: (v[:160] if k == "description" else v) for k, v in asset.items()
                  if k != "technique"} for output in outputs for asset in output["assets"]]
        budget = min(14000, 28000 - len(schema) - len(RULES)
                     - len(json.dumps(known + prior, ensure_ascii=False)))
        if budget < 1000:
            raise RuntimeError("项目资产目录超出提取上下文预算，请先整理重复资产；已完成批次已保存")
        remaining = batches(rows[offset:], budget)
        group = remaining[0]
        if index < len(completed):
            output = Extraction.model_validate(completed[index])
        else:
            await worker.record_progress(task_id, 73 + int(15 * offset / len(rows)),
                f"统一提取分镜资产：第 {index + 1} 批，已处理 {offset}/{len(rows)} 镜，前批结果用于去重")
            prompt = (RULES + "\n招式结构：" + schema
                + '\n输出：{"assets":[],"shots":[{"order_index":1,"asset_names":["已有资产名"]}]}'
                + "\n项目目录：" + json.dumps(known, ensure_ascii=False)
                + "\n前批已确认资产：" + json.dumps(prior, ensure_ascii=False)
                + "\n本批完整分镜：" + json.dumps(group, ensure_ascii=False))
            if len(prompt) > 32000:
                raise RuntimeError("分镜资产提取累计目录超过单次预算，已保留完成批次，请精简资产描述后重试")
            request = await worker.runtime_request(task_id, prompt_code="script-asset-extraction", prompt=prompt)
            request.system_prompt += "\n" + RULES
            request.session_id += f"-storyboard-assets-{index}"
            response = await runtime_factory().run(request)
            output = Extraction.model_validate(worker.parse_json_object(response.final_response))
        data = output.model_dump(mode="json")
        expected = {row["order_index"] for row in group}
        if {s.order_index for s in output.shots} != expected or len(output.shots) != len(group):
            raise RuntimeError("分镜资产提取返回的镜头关联不完整")
        available = {a["name"] for a in known + prior + data["assets"]}
        if any(name not in available for shot in output.shots for name in shot.asset_names):
            raise RuntimeError("分镜资产提取引用了未定义资产")
        for asset in output.assets:
            if asset.parent_name and asset.parent_name not in available:
                raise RuntimeError("分镜衍生资产缺少基础身份")
            if asset.technique and (not asset.parent_name or asset.asset_type != "character"):
                raise RuntimeError("招式必须绑定所属人物")
        parents = {a["name"]: a.get("parent_name") for a in known + prior + data["assets"]}
        for shot in data["shots"]:
            shot["asset_names"] = list(dict.fromkeys(shot["asset_names"] + [
                parents[name] for name in shot["asset_names"] if parents.get(name)]))
        outputs.append(data)
        offset += len(group)
        if index >= len(completed):
            async with worker.SessionLocal() as session:
                task = await worker.owned_task_for_update(session, task_id)
                if not worker.owns_running_task(task):
                    raise RuntimeError("分镜资产提取任务已停止")
                task.request_payload = {**task.request_payload, "storyboard_asset_batches": {
                    "fingerprint": fingerprint, "catalog": known, "completed": outputs}}
                await session.commit()
    bindings = {s["order_index"]: s["asset_names"] for result in outputs for s in result["shots"]}
    return [a for result in outputs for a in result["assets"]], bindings


async def apply(session, task, extraction, definitions, bindings):
    """Commit under the same transaction as the storyboard; roots before variants."""
    await session.execute(update(Project).where(Project.id == task.project_id).values(name=Project.name))
    catalog = await extraction_asset_catalog(session, task.project_id, task.tenant_id, task.user_id)
    linked = set((await session.scalars(select(AssetExtractionItem.asset_id).where(
        AssetExtractionItem.extraction_id == extraction.id))).all())
    aliases = {}
    for row in sorted(definitions, key=lambda a: bool(a.get("parent_name"))):
        parent = None
        if row.get("parent_name"):
            parent = reusable_asset(catalog, row["asset_type"], row["parent_name"], None)
            if parent is None:
                raise RuntimeError(f"衍生资产缺少同类型基础资产：{row['parent_name']}")
        asset = reusable_asset(catalog, row["asset_type"], row["name"], parent.id if parent else None)
        if asset is None:
            if any(asset_name_key(a.name) == asset_name_key(row["name"]) for a in catalog):
                raise RuntimeError(f"资产同名但身份或主资产不一致：{row['name']}，请核对分镜资产定义")
            technique = row.get("technique")
            asset = Asset(tenant_id=task.tenant_id, user_id=task.user_id, project_id=task.project_id,
                scope=AssetScope.PROJECT, asset_type=AssetType(row["asset_type"]), name=row["name"],
                description=row["description"], parent_asset_id=parent.id if parent else None,
                generation_prompt=technique["image_prompt"] if technique else "", status=AssetStatus.EXTRACTED,
                asset_metadata={"source_task_id": task.id, "extraction_id": extraction.id,
                    "source_stage": "storyboard", **({"combat_technique": technique} if technique else {})})
            session.add(asset)
            await session.flush()
            await snapshot_asset_revision(session, asset, change_type="ai_extraction", source_task_id=task.id)
            catalog.append(asset)
        if asset.id not in linked:
            session.add(AssetExtractionItem(extraction_id=extraction.id, asset_id=asset.id))
            linked.add(asset.id)
        aliases[row["name"]] = asset
    by_name = {a.name: a for a in reversed(catalog)}
    by_name.update(aliases)
    for name in {name for names in bindings.values() for name in names}:
        asset = by_name[name]
        if asset.id not in linked:
            session.add(AssetExtractionItem(extraction_id=extraction.id, asset_id=asset.id))
            linked.add(asset.id)
    return by_name
