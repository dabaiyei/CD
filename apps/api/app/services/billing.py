from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AITask, CreditAccount, CreditLedger, PricingRule


@dataclass(frozen=True)
class PricingDefinition:
    task_type: str
    name: str
    description: str
    unit_label: str
    default_unit_cost: Decimal
    display_order: int


@dataclass(frozen=True)
class ResolvedTaskPricing:
    task_type: str
    unit_cost: Decimal
    quantity: int
    total_cost: Decimal
    rule_version: int

    def as_payload(self) -> dict[str, str | int]:
        payload = asdict(self)
        payload["unit_cost"] = str(self.unit_cost)
        payload["total_cost"] = str(self.total_cost)
        return payload


DEFAULT_PRICING_RULES = (
    PricingDefinition("agent_chat_run", "Agent 对话", "创作台 Agent 多轮协作运行", "轮", Decimal("0"), 10),
    PricingDefinition(
        "project_cover_generation",
        "项目封面",
        "根据项目与视觉手册生成封面",
        "张",
        Decimal("20"),
        20,
    ),
    PricingDefinition(
        "chapter_analysis_generation",
        "章节分析",
        "提炼事件、冲突与改编策略",
        "版本",
        Decimal("5"),
        30,
    ),
    PricingDefinition(
        "chapter_script_generation",
        "AI 剧本",
        "生成新的待审核短剧剧本",
        "版本",
        Decimal("10"),
        40,
    ),
    PricingDefinition(
        "chapter_asset_extraction",
        "资产提取",
        "从生效剧本提取人物、场景与道具",
        "版本",
        Decimal("8"),
        50,
    ),
    PricingDefinition(
        "asset_prompt_generation",
        "资产提示词",
        "为塑造资产生成生图提示词",
        "资产",
        Decimal("2"),
        60,
    ),
    PricingDefinition(
        "asset_image_generation",
        "资产图片",
        "根据资产提示词生成定稿图",
        "张",
        Decimal("20"),
        70,
    ),
    PricingDefinition(
        "chapter_storyboard_generation",
        "AI 分镜",
        "生成可拍摄的章节分镜表",
        "版本",
        Decimal("12"),
        80,
    ),
    PricingDefinition(
        "shot_video_prompt_generation",
        "镜头视频提示词",
        "为分镜镜头生成或重写视频提示词",
        "镜头",
        Decimal("2"),
        85,
    ),
    PricingDefinition(
        "shot_video_generation",
        "镜头视频",
        "根据单镜头提示词生成视频",
        "镜头",
        Decimal("60"),
        90,
    ),
    PricingDefinition(
        "chapter_dialogue_extraction",
        "台词提取",
        "提取角色台词与表演指导",
        "版本",
        Decimal("4"),
        100,
    ),
    PricingDefinition(
        "dialogue_tts_generation",
        "台词配音",
        "按角色音色生成单条台词音频",
        "条",
        Decimal("5"),
        110,
    ),
)
DEFAULT_PRICING_BY_TASK = {item.task_type: item for item in DEFAULT_PRICING_RULES}


async def ensure_pricing_rules(session: AsyncSession, tenant_id: str) -> None:
    existing = set(
        (await session.scalars(select(PricingRule.task_type).where(PricingRule.tenant_id == tenant_id))).all()
    )
    session.add_all(
        [
            PricingRule(
                tenant_id=tenant_id,
                task_type=item.task_type,
                name=item.name,
                description=item.description,
                unit_label=item.unit_label,
                unit_cost=item.default_unit_cost,
                display_order=item.display_order,
            )
            for item in DEFAULT_PRICING_RULES
            if item.task_type not in existing
        ]
    )


async def resolve_task_pricing(
    session: AsyncSession,
    *,
    tenant_id: str,
    task_type: str,
    quantity: int = 1,
) -> ResolvedTaskPricing:
    if quantity < 1:
        raise ValueError("pricing quantity must be positive")
    rule = await session.scalar(
        select(PricingRule).where(
            PricingRule.tenant_id == tenant_id,
            PricingRule.task_type == task_type,
        )
    )
    if rule is None:
        definition = DEFAULT_PRICING_BY_TASK.get(task_type)
        if definition is None:
            raise RuntimeError(f"pricing rule is not defined for {task_type}")
        unit_cost = definition.default_unit_cost
        version = 0
    else:
        unit_cost = rule.unit_cost
        version = rule.version
    return ResolvedTaskPricing(
        task_type=task_type,
        unit_cost=unit_cost,
        quantity=quantity,
        total_cost=unit_cost * quantity,
        rule_version=version,
    )


async def debit_task_cost(
    session: AsyncSession,
    task: AITask,
    *,
    reason: str,
) -> None:
    if task.cost <= 0:
        return
    account = await session.scalar(
        select(CreditAccount)
        .where(CreditAccount.tenant_id == task.tenant_id, CreditAccount.user_id == task.user_id)
        .with_for_update()
    )
    if account is None or account.balance < task.cost:
        raise HTTPException(status_code=402, detail="积分不足，无法提交任务")
    account.balance -= task.cost
    session.add(
        CreditLedger(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            amount=-task.cost,
            balance_after=account.balance,
            reason=reason,
            reference_type="ai_task",
            reference_id=task.id,
        )
    )


async def refund_task_cost(
    session: AsyncSession,
    task: AITask,
    *,
    reason: str,
) -> bool:
    result = dict(task.result_payload or {})
    if result.get("credit_refunded") or task.cost <= Decimal("0"):
        return False

    account = await session.scalar(
        select(CreditAccount)
        .where(CreditAccount.tenant_id == task.tenant_id, CreditAccount.user_id == task.user_id)
        .with_for_update()
    )
    if account is None:
        raise RuntimeError("credit account missing while refunding task")
    account.balance += task.cost
    result["credit_refunded"] = True
    task.result_payload = result
    session.add(
        CreditLedger(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            amount=task.cost,
            balance_after=account.balance,
            reason=reason,
            reference_type="ai_task",
            reference_id=task.id,
        )
    )
    return True
