"""Persist original AI project configuration and creation conversation state."""

import sqlalchemy as sa
from alembic import op

revision = "ab83e2196c40"
down_revision = "e4a7c1d9b620"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive changes avoid SQLite table recreation with existing chapter FKs.
    op.add_column(
        "projects", sa.Column("creation_mode", sa.String(16), nullable=False, server_default="import")
    )
    op.add_column("projects", sa.Column("cinematic", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("projects", sa.Column("creation_state", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("projects", sa.Column("text_model_id", sa.String(36), nullable=True))


def downgrade() -> None:
    for name in ("text_model_id", "creation_state", "cinematic", "creation_mode"):
        op.drop_column("projects", name)
