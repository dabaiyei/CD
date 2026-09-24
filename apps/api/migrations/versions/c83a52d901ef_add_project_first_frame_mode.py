"""Make previous-video tail-frame chaining an explicit project option."""

import sqlalchemy as sa
from alembic import op

revision = "c83a52d901ef"
down_revision = "b7c41e9a52f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("first_frame_mode", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("projects", "first_frame_mode")
