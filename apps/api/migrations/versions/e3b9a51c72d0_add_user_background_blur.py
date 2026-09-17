"""Persist account background blur; existing accounts default to zero."""

from alembic import op
import sqlalchemy as sa

revision = "e3b9a51c72d0"
down_revision = "ab83e2196c40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("background_blur", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("background_blur")
