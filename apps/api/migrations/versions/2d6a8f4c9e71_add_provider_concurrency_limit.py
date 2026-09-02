"""add provider concurrency limit

Revision ID: 2d6a8f4c9e71
Revises: 6b2e4f8a1c90
Create Date: 2026-08-31 23:59:30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d6a8f4c9e71"
down_revision: str | Sequence[str] | None = "6b2e4f8a1c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="2")
        )


def downgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("max_concurrency")
