"""Allow projects to bypass creative review and its repair cycle."""

import sqlalchemy as sa
from alembic import op

revision = "a17b8c920e43"
down_revision = "f4c8a61d9032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("review_enabled", sa.Boolean(), nullable=False,
                                      server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("projects", "review_enabled")
