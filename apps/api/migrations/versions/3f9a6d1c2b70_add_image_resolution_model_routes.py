"""add image resolution model routes

Revision ID: 3f9a6d1c2b70
Revises: 8d4e1f6a2b90
Create Date: 2026-09-03 14:00:00

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "3f9a6d1c2b70"
down_revision: str | Sequence[str] | None = "8d4e1f6a2b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_resolution_model_routes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("resolution", sa.String(length=8), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["ai_models.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "resolution",
            name="uq_image_resolution_route_tenant_resolution",
        ),
    )
    op.create_index(
        op.f("ix_image_resolution_model_routes_model_id"),
        "image_resolution_model_routes",
        ["model_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_image_resolution_model_routes_tenant_id"),
        "image_resolution_model_routes",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_image_resolution_routes_tenant_model",
        "image_resolution_model_routes",
        ["tenant_id", "model_id"],
        unique=False,
    )

    connection = op.get_bind()
    models = sa.table(
        "ai_models",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("model_type", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("is_default", sa.Boolean),
    )
    routes = sa.table(
        "image_resolution_model_routes",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("resolution", sa.String),
        sa.column("model_id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    defaults = connection.execute(
        sa.select(models.c.tenant_id, models.c.id)
        .where(
            models.c.model_type == "IMAGE",
            models.c.enabled.is_(True),
            models.c.is_default.is_(True),
        )
        .order_by(models.c.tenant_id, models.c.id)
    ).all()
    now = datetime.now(UTC)
    seen_tenants: set[str] = set()
    rows: list[dict[str, object]] = []
    for tenant_id, model_id in defaults:
        if tenant_id in seen_tenants:
            continue
        seen_tenants.add(tenant_id)
        rows.extend(
            {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "resolution": resolution,
                "model_id": model_id,
                "created_at": now,
                "updated_at": now,
            }
            for resolution in ("1K", "2K", "4K")
        )
    if rows:
        connection.execute(routes.insert(), rows)


def downgrade() -> None:
    op.drop_index(
        "ix_image_resolution_routes_tenant_model",
        table_name="image_resolution_model_routes",
    )
    op.drop_index(
        op.f("ix_image_resolution_model_routes_tenant_id"),
        table_name="image_resolution_model_routes",
    )
    op.drop_index(
        op.f("ix_image_resolution_model_routes_model_id"),
        table_name="image_resolution_model_routes",
    )
    op.drop_table("image_resolution_model_routes")
