"""add script review audit

Revision ID: d91b46a7e305
Revises: c7e31f8a942d
Create Date: 2026-09-01 01:25:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d91b46a7e305"
down_revision: str | Sequence[str] | None = "c7e31f8a942d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "script_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("script_version_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=17), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("reviewer_name", sa.String(length=80), nullable=False),
        sa.Column("activated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["script_version_id"], ["script_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_script_reviews_chapter_id"), "script_reviews", ["chapter_id"])
    op.create_index(op.f("ix_script_reviews_decision"), "script_reviews", ["decision"])
    op.create_index(op.f("ix_script_reviews_project_id"), "script_reviews", ["project_id"])
    op.create_index(
        op.f("ix_script_reviews_script_version_id"), "script_reviews", ["script_version_id"]
    )
    op.create_index(op.f("ix_script_reviews_tenant_id"), "script_reviews", ["tenant_id"])
    op.create_index(op.f("ix_script_reviews_user_id"), "script_reviews", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_script_reviews_user_id"), table_name="script_reviews")
    op.drop_index(op.f("ix_script_reviews_tenant_id"), table_name="script_reviews")
    op.drop_index(op.f("ix_script_reviews_script_version_id"), table_name="script_reviews")
    op.drop_index(op.f("ix_script_reviews_project_id"), table_name="script_reviews")
    op.drop_index(op.f("ix_script_reviews_decision"), table_name="script_reviews")
    op.drop_index(op.f("ix_script_reviews_chapter_id"), table_name="script_reviews")
    op.drop_table("script_reviews")
