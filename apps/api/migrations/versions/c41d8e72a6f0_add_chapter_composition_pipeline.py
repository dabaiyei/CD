"""add chapter composition pipeline

Revision ID: c41d8e72a6f0
Revises: 9c067b4c7763
Create Date: 2026-08-31 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c41d8e72a6f0"
down_revision: str | Sequence[str] | None = "9c067b4c7763"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "composition_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("storyboard_version_id", sa.String(length=36), nullable=False),
        sa.Column("dialogue_version_id", sa.String(length=36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=False),
        sa.Column("fps", sa.Integer(), nullable=False),
        sa.Column("timeline_manifest", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "RENDERING",
                "READY",
                "FAILED",
                "STALE",
                name="compositionstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("output_url", sa.String(length=600), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("invalidated_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["dialogue_version_id"], ["dialogue_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["storyboard_version_id"], ["storyboard_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chapter_id", "version", name="uq_composition_chapter_version"),
    )
    with op.batch_alter_table("composition_versions", schema=None) as batch_op:
        for column in (
            "chapter_id",
            "dialogue_version_id",
            "is_active",
            "project_id",
            "status",
            "storyboard_version_id",
            "tenant_id",
            "user_id",
        ):
            batch_op.create_index(batch_op.f(f"ix_composition_versions_{column}"), [column], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("composition_versions", schema=None) as batch_op:
        for column in reversed(
            (
                "chapter_id",
                "dialogue_version_id",
                "is_active",
                "project_id",
                "status",
                "storyboard_version_id",
                "tenant_id",
                "user_id",
            )
        ):
            batch_op.drop_index(batch_op.f(f"ix_composition_versions_{column}"))
    op.drop_table("composition_versions")
