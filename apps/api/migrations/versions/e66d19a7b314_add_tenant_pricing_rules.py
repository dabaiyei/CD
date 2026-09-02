"""add tenant pricing rules

Revision ID: e66d19a7b314
Revises: d82f41c0a63e
Create Date: 2026-08-31 23:40:00

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "e66d19a7b314"
down_revision: str | Sequence[str] | None = "d82f41c0a63e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DEFAULT_RULES = (
    ("agent_chat_run", "Agent 对话", "创作台 Agent 多轮协作运行", "轮", Decimal("0"), 10),
    ("project_cover_generation", "项目封面", "根据项目与视觉手册生成封面", "张", Decimal("20"), 20),
    ("chapter_analysis_generation", "章节分析", "提炼事件、冲突与改编策略", "版本", Decimal("5"), 30),
    ("chapter_script_generation", "AI 剧本", "生成新的待审核短剧剧本", "版本", Decimal("10"), 40),
    ("chapter_asset_extraction", "资产提取", "从生效剧本提取人物、场景与道具", "版本", Decimal("8"), 50),
    ("asset_prompt_generation", "资产提示词", "为塑造资产生成生图提示词", "资产", Decimal("2"), 60),
    ("asset_image_generation", "资产图片", "根据资产提示词生成定稿图", "张", Decimal("20"), 70),
    ("chapter_storyboard_generation", "AI 分镜", "生成可拍摄的章节分镜表", "版本", Decimal("12"), 80),
    ("shot_video_generation", "镜头视频", "根据单镜头提示词生成视频", "镜头", Decimal("60"), 90),
    ("chapter_dialogue_extraction", "台词提取", "提取角色台词与表演指导", "版本", Decimal("4"), 100),
    ("dialogue_tts_generation", "台词配音", "按角色音色生成单条台词音频", "条", Decimal("5"), 110),
)


def upgrade() -> None:
    pricing_rules = op.create_table(
        "pricing_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("unit_label", sa.String(length=40), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "task_type", name="uq_pricing_rule_task_type"),
    )
    op.create_index(op.f("ix_pricing_rules_task_type"), "pricing_rules", ["task_type"], unique=False)
    op.create_index(op.f("ix_pricing_rules_tenant_id"), "pricing_rules", ["tenant_id"], unique=False)

    tenant_ids = list(op.get_bind().execute(sa.text("SELECT id FROM tenants")).scalars())
    now = datetime.now(UTC)
    op.bulk_insert(
        pricing_rules,
        [
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "task_type": task_type,
                "name": name,
                "description": description,
                "unit_label": unit_label,
                "unit_cost": unit_cost,
                "display_order": display_order,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            for tenant_id in tenant_ids
            for task_type, name, description, unit_label, unit_cost, display_order in DEFAULT_RULES
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_pricing_rules_tenant_id"), table_name="pricing_rules")
    op.drop_index(op.f("ix_pricing_rules_task_type"), table_name="pricing_rules")
    op.drop_table("pricing_rules")
