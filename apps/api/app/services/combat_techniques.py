"""Character-owned technique assets are persistent, versioned combat memory."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select

from app.db.models import Asset, AssetScope, AssetStatus, AssetType
from app.services.asset_revisions import snapshot_asset_revision


class TechniqueDesignRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=4000)
    count: int = Field(default=3, ge=1, le=6)


async def queue_design(session, user, owner, payload):
    from fastapi import HTTPException
    from app.db.models import Project
    from app.services.asset_tasks import resolve_general_agent_text_model
    from app.services.billing import resolve_task_pricing
    from app.services.task_submission import active_tasks, create_queued_task
    # Serialize submissions for this character before checking active tasks.
    owner = await session.scalar(select(Asset).where(Asset.id == owner.id).with_for_update())
    if owner is None:
        raise HTTPException(404, "人物已删除")
    if owner.user_id != user.id or owner.tenant_id != user.tenant_id or owner.scope != AssetScope.PROJECT or owner.asset_type != AssetType.CHARACTER or (owner.asset_metadata or {}).get("combat_technique"):
        raise HTTPException(422, "请选择自己项目中的人物设计招式")
    project = await session.get(Project, owner.project_id)
    if project is None or project.owner_id != user.id:
        raise HTTPException(404, "项目不可用")
    pending = await active_tasks(session, project_id=project.id, task_type="character_technique_design")
    if any(t.request_payload.get("asset_id") == owner.id for t in pending):
        raise HTTPException(409, "该人物的招式正在设计")
    agent, model, _, _ = await resolve_general_agent_text_model(session, user.tenant_id, project)
    pricing = await resolve_task_pricing(session, tenant_id=user.tenant_id, task_type="asset_prompt_generation", quantity=payload.count)
    return await create_queued_task(session, user=user, project_id=project.id,
        task_type="character_technique_design", model_id=model.id, cost=pricing.total_cost,
        request_payload={"agent_profile_id": agent.id, "asset_id": owner.id, "asset_version": owner.version, "design": payload.model_dump(), "pricing": pricing.as_payload()},
        message=f"{owner.name}的招式设计")


class CombatTechnique(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["招式", "大招", "宝术", "神诀", "召唤", "法相"]
    signature: str = Field(min_length=1, max_length=1200)
    activation: str = Field(min_length=1, max_length=1200)
    action_chain: list[str] = Field(min_length=3, max_length=8)
    impact: str = Field(min_length=1, max_length=1200)
    recovery: str = Field(min_length=1, max_length=1200)
    visual_identity: str = Field(min_length=1, max_length=1800)
    scale_anchor: str = Field(min_length=1, max_length=800)
    limitations: str = Field(min_length=1, max_length=800)
    image_prompt: str = Field(min_length=1, max_length=8000)

    def memory(self) -> str:
        return "\n".join([
            f"【{self.kind}】{self.name}｜{self.signature}",
            f"起手：{self.activation}",
            "动作链：" + " → ".join(self.action_chain),
            f"命中反馈：{self.impact}", f"收势：{self.recovery}",
            f"视觉身份：{self.visual_identity}", f"尺度锚点：{self.scale_anchor}",
            f"使用条件与代价：{self.limitations}",
        ])


class TechniqueDesignResult(BaseModel):
    techniques: list[CombatTechnique] = Field(min_length=1, max_length=6)


TECHNIQUE_IMAGE_RULES = (
    "\n【招式图专用约束，优先于人物与衍生形态制图规则】"
    "只画招式特效、武器、能量结构或召唤物本体，不画施术者、对手、人体局部、手、脸、"
    "人物剪影或人体比例尺。人形法相只能作为能量造型，不能画成另一个真人或复刻人物脸。"
    "旧描述中的人物、持握、施放姿势与战场站位仅作为归属和运动说明，不在图中呈现；"
    "武器独立展示，能量入口、延展方向、轮廓、材质、纹样、配色清楚，以简洁背景衬托，"
    "不画具体战场、不加文字、箭头标注、四视图或动作拼贴。"
    "该图不决定人物性别、脸、服装、站位或镜头构图；与人物的绑定在视频提示词中说明。"
)


TECHNIQUE_RULES = """
你是角色战斗能力设计师。以项目画风和世界观为准，不将 UE5、动漫或固定秒数强加给所有项目。
为指定人物设计可反复识别的独有招式：主战、破招、终结各有用途；宝术与神诀要有独有手印、
发力路径、能量纹样与配色。大招安排起势/显形、施放/攻防、命中/收束的清晰因果，不堆光污染。
召唤巨龙应固定龙角、鳞片、颜色、召唤入口、与施术者关系及目标；法天象地应固定放大倍率、
地标尺度、法相面貌、与本体动作同步关系。动作链明确主攻部位、轨迹、对手反应、重量与环境反馈。
特效绑定发力点与轨迹；普通交锋高速实时，回弹借力连续变招，不逐招停顿。
只有大招蓄势显形与关键命中展示才短暂放慢，随后恢复实时，不用瞬移、定格轮播假装流畅。
image_prompt 为单张无人招式视觉设定参考图：只展示特效、武器、能量形态或召唤物，不画施术者和对手。
该图约束招式身份，不冒充视频的0秒首帧，不另外生成独立打斗首帧；同场接续由前镜真实尾帧完成。
已有招式是长期记忆：不改名、不变色、不换武器、不改变龙/法相身份；仅显式要求才修改。
不擅加技能喊话或外语。技能名称不是必须显示或朗读的字幕。
""" + TECHNIQUE_IMAGE_RULES


async def technique_catalog(session, project_id: str, user_id: str) -> list[Asset]:
    rows = (await session.scalars(select(Asset).where(
        Asset.project_id == project_id, Asset.user_id == user_id,
        Asset.scope == AssetScope.PROJECT,
    ))).all()
    return [a for a in rows if (a.asset_metadata or {}).get("combat_technique")]


async def save_techniques(session, owner: Asset, techniques: list[CombatTechnique], task_id: str) -> list[Asset]:
    if owner.asset_type != AssetType.CHARACTER or owner.scope != AssetScope.PROJECT:
        raise ValueError("招式只能绑定项目人物")
    existing = await technique_catalog(session, owner.project_id, owner.user_id)
    names = {a.name for a in existing if a.parent_asset_id == owner.id}
    if len({t.name for t in techniques}) != len(techniques):
        raise ValueError("AI 返回重复招式名称")
    created = []
    for technique in techniques:
        name = f"{owner.name}·{technique.name}"[:160]
        if name in names:
            raise ValueError(f"人物已有招式 {name}，请编辑已有招式")
        asset = Asset(
            tenant_id=owner.tenant_id, user_id=owner.user_id, project_id=owner.project_id,
            scope=AssetScope.PROJECT, asset_type=AssetType.CHARACTER, parent_asset_id=owner.id,
            name=name, description=technique.memory(), generation_prompt=technique.image_prompt,
            status=AssetStatus.PROMPT_READY,
            asset_metadata={"combat_technique": technique.model_dump(), "source_task_id": task_id},
        )
        session.add(asset)
        await session.flush()
        await snapshot_asset_revision(session, asset, change_type="technique_design", source_task_id=task_id)
        created.append(asset)
    return created


async def execute(task_id, runtime_factory):
    from app.services import task_worker as worker
    from app.db.models import TaskStatus
    async with worker.SessionLocal() as session:
        task = await worker.owned_task_for_update(session, task_id)
        if not worker.owns_running_task(task):
            return
        owner = await session.get(Asset, task.request_payload["asset_id"])
        if owner is None or owner.user_id != task.user_id or owner.project_id != task.project_id:
            raise RuntimeError("招式所属人物已不存在")
        catalog = await technique_catalog(session, task.project_id, task.user_id)
        prompt = TECHNIQUE_RULES + "\n" + json.dumps({
            "character": {"name": owner.name, "description": owner.description},
            "request": task.request_payload["design"],
            "existing_techniques": [{"name": a.name, "description": a.description} for a in catalog if a.parent_asset_id == owner.id],
            "output_schema": TechniqueDesignResult.model_json_schema(),
        }, ensure_ascii=False) + "\n仅返回符合 output_schema 的 JSON。"
    request = await worker.runtime_request(task_id, prompt_code="character-technique-design", prompt=prompt)
    result = await runtime_factory().run(request)
    parsed = TechniqueDesignResult.model_validate(worker.parse_json_object(result.final_response))
    async with worker.SessionLocal() as session:
        task = await worker.owned_task_for_update(session, task_id)
        if not worker.owns_running_task(task):
            return
        owner = await session.scalar(select(Asset).where(Asset.id == task.request_payload["asset_id"]).with_for_update())
        if owner is None or owner.version != task.request_payload["asset_version"]:
            raise RuntimeError("人物已编辑或删除，请重新设计招式")
        if len(parsed.techniques) != task.request_payload["design"]["count"]:
            raise RuntimeError("AI 返回的招式数量与请求不一致")
        assets = await save_techniques(session, owner, parsed.techniques, task.id)
        task.status = TaskStatus.SUCCEEDED
        task.result_payload = {"asset_ids": [a.id for a in assets], "character_id": owner.id}
        event = worker.record_task_event(session, task, status=TaskStatus.SUCCEEDED, progress=100,
            message=f"{owner.name}的 {len(assets)} 个招式已保存，可生成参考图")
        await session.commit()
        await worker.publish_task_event(task, event)
