"""add automatic director workflow state

Revision ID: e4a7c1d9b620
Revises: 5c8d1f7a4b20
Create Date: 2026-09-05 00:40:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7c1d9b620"
down_revision: str | Sequence[str] | None = "5c8d1f7a4b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "director_workflow_runs",
        sa.Column("automation_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "director_workflow_runs",
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        op.f("ix_director_workflow_runs_automation_mode"),
        "director_workflow_runs",
        ["automation_mode"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_director_workflow_runs_automation_mode"),
        table_name="director_workflow_runs",
    )
    op.drop_column("director_workflow_runs", "stop_requested")
    op.drop_column("director_workflow_runs", "automation_mode")
