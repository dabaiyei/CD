"""persist the chapter's technique plan on script versions

Revision ID: b7c41e9a52f8
Revises: e3b9a51c72d0
Create Date: 2026-09-20 15:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c41e9a52f8"
down_revision: str | Sequence[str] | None = "e3b9a51c72d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing versions have no plan; later stages treat that as "no techniques
    # declared" and keep their previous behaviour.
    op.add_column(
        "script_versions",
        sa.Column("technique_plan", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    with op.batch_alter_table("script_versions") as batch_op:
        batch_op.drop_column("technique_plan")
