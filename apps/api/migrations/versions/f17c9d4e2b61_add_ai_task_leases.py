"""add explicit ai task leases and execution timestamps

Revision ID: f17c9d4e2b61
Revises: c41d8e72a6f0
Create Date: 2026-08-31 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f17c9d4e2b61"
down_revision: str | Sequence[str] | None = "c41d8e72a6f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ai_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("worker_id", sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_ai_tasks_lease_expires_at"), ["lease_expires_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("ai_tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ai_tasks_lease_expires_at"))
        batch_op.drop_column("completed_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("worker_id")
