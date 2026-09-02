"""add user avatars

Revision ID: 8d4e1f6a2b90
Revises: 7c9e4b2a1f60
Create Date: 2026-09-03 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d4e1f6a2b90"
down_revision: str | Sequence[str] | None = "7c9e4b2a1f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("avatar_url", sa.String(length=600), nullable=True))
        batch_op.add_column(
            sa.Column("avatar_storage_path", sa.String(length=600), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("avatar_storage_path")
        batch_op.drop_column("avatar_url")
