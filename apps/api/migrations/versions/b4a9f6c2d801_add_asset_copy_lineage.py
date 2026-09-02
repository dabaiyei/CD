"""add asset copy lineage

Revision ID: b4a9f6c2d801
Revises: e66d19a7b314
Create Date: 2026-08-31 23:58:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4a9f6c2d801"
down_revision: str | Sequence[str] | None = "e66d19a7b314"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lineage_id", sa.String(length=36), nullable=True))
    assets = sa.table(
        "assets",
        sa.column("id", sa.String(length=36)),
        sa.column("lineage_id", sa.String(length=36)),
    )
    op.execute(assets.update().values(lineage_id=assets.c.id))
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.alter_column("lineage_id", existing_type=sa.String(length=36), nullable=False)
        batch_op.create_index(batch_op.f("ix_assets_lineage_id"), ["lineage_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_assets_lineage_id"))
        batch_op.drop_column("lineage_id")
