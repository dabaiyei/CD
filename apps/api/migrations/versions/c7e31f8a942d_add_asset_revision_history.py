"""add asset revision history

Revision ID: c7e31f8a942d
Revises: b4a9f6c2d801
Create Date: 2026-09-01 00:40:00

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e31f8a942d"
down_revision: str | Sequence[str] | None = "b4a9f6c2d801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    revisions = op.create_table(
        "asset_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=80), nullable=False),
        sa.Column("source_task_id", sa.String(length=36), nullable=True),
        sa.Column("source_revision_id", sa.String(length=36), nullable=True),
        sa.Column("parent_asset_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("generation_prompt", sa.Text(), nullable=False),
        sa.Column("media_url", sa.String(length=600), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("asset_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "version", name="uq_asset_revision_version"),
    )
    op.create_index(op.f("ix_asset_revisions_asset_id"), "asset_revisions", ["asset_id"], unique=False)
    op.create_index(
        op.f("ix_asset_revisions_change_type"), "asset_revisions", ["change_type"], unique=False
    )
    op.create_index(
        op.f("ix_asset_revisions_source_task_id"),
        "asset_revisions",
        ["source_task_id"],
        unique=False,
    )
    op.create_index(op.f("ix_asset_revisions_status"), "asset_revisions", ["status"], unique=False)
    op.create_index(op.f("ix_asset_revisions_tenant_id"), "asset_revisions", ["tenant_id"], unique=False)

    bind = op.get_bind()
    assets = sa.table(
        "assets",
        sa.column("id", sa.String(length=36)),
        sa.column("tenant_id", sa.String(length=36)),
        sa.column("version", sa.Integer()),
        sa.column("parent_asset_id", sa.String(length=36)),
        sa.column("name", sa.String(length=160)),
        sa.column("description", sa.Text()),
        sa.column("generation_prompt", sa.Text()),
        sa.column("media_url", sa.String(length=600)),
        sa.column("status", sa.String(length=12)),
        sa.column("asset_metadata", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(sa.select(assets)).mappings().all()
    if rows:
        op.bulk_insert(
            revisions,
            [
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": row["tenant_id"],
                    "asset_id": row["id"],
                    "version": row["version"],
                    "change_type": "migration_snapshot",
                    "source_task_id": None,
                    "source_revision_id": None,
                    "parent_asset_id": row["parent_asset_id"],
                    "name": row["name"],
                    "description": row["description"],
                    "generation_prompt": row["generation_prompt"],
                    "media_url": row["media_url"],
                    "status": row["status"],
                    "asset_metadata": row["asset_metadata"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_asset_revisions_tenant_id"), table_name="asset_revisions")
    op.drop_index(op.f("ix_asset_revisions_status"), table_name="asset_revisions")
    op.drop_index(op.f("ix_asset_revisions_source_task_id"), table_name="asset_revisions")
    op.drop_index(op.f("ix_asset_revisions_change_type"), table_name="asset_revisions")
    op.drop_index(op.f("ix_asset_revisions_asset_id"), table_name="asset_revisions")
    op.drop_table("asset_revisions")
