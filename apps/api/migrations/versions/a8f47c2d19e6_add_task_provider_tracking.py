"""add explicit task idempotency and provider tracking

Revision ID: a8f47c2d19e6
Revises: d91b46a7e305
Create Date: 2026-09-01 02:20:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8f47c2d19e6"
down_revision: str | Sequence[str] | None = "d91b46a7e305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ai_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("provider_job_id", sa.String(length=255), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_ai_tasks_idempotency_key"),
            ["idempotency_key"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_ai_tasks_provider_job_id"),
            ["provider_job_id"],
            unique=False,
        )

    op.execute(sa.text("UPDATE ai_tasks SET idempotency_key = id WHERE idempotency_key IS NULL"))


def downgrade() -> None:
    with op.batch_alter_table("ai_tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ai_tasks_provider_job_id"))
        batch_op.drop_index(batch_op.f("ix_ai_tasks_idempotency_key"))
        batch_op.drop_column("provider_job_id")
        batch_op.drop_column("idempotency_key")
