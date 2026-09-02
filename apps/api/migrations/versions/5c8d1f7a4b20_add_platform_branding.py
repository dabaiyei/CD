"""add platform branding

Revision ID: 5c8d1f7a4b20
Revises: 1e7c4a9b2d60
Create Date: 2026-09-03 12:00:00

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "5c8d1f7a4b20"
down_revision: str | Sequence[str] | None = "1e7c4a9b2d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    now = datetime.now(UTC)
    platform_branding = op.create_table(
        "platform_branding",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("login_background_video_source", sa.String(length=16), nullable=False),
        sa.Column("login_background_video_url", sa.String(length=2000), nullable=True),
        sa.Column("login_background_video_storage_path", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        platform_branding,
        [
            {
                "id": "default",
                "login_background_video_source": "default",
                "login_background_video_url": None,
                "login_background_video_storage_path": None,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("platform_branding")
