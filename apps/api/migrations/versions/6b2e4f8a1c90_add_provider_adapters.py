"""add encrypted provider credentials and declarative adapters

Revision ID: 6b2e4f8a1c90
Revises: 71ad6f0c8b32
Create Date: 2026-08-31 23:59:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b2e4f8a1c90"
down_revision: str | Sequence[str] | None = "71ad6f0c8b32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("encrypted_credentials", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("adapter_config", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("adapter_config")
        batch_op.drop_column("encrypted_credentials")
