"""add model connectivity diagnostics

Revision ID: d82f41c0a63e
Revises: a53b912dd872
Create Date: 2026-08-31 23:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d82f41c0a63e"
down_revision: str | Sequence[str] | None = "a53b912dd872"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ai_models", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_test_ok", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("last_test_message", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("last_test_latency_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ai_models", schema=None) as batch_op:
        batch_op.drop_column("last_test_latency_ms")
        batch_op.drop_column("last_test_message")
        batch_op.drop_column("last_test_ok")
        batch_op.drop_column("last_tested_at")
