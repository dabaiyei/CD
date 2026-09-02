"""add user skills and personal agent sessions

Revision ID: 7c9e4b2a1f60
Revises: 2d6a8f4c9e71
Create Date: 2026-09-01 10:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c9e4b2a1f60"
down_revision: str | Sequence[str] | None = "2d6a8f4c9e71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_skills",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("trigger_stages", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "user_id", "name", name="uq_user_skill_name"),
    )
    op.create_index(op.f("ix_user_skills_tenant_id"), "user_skills", ["tenant_id"])
    op.create_index(op.f("ix_user_skills_user_id"), "user_skills", ["user_id"])
    op.create_index(op.f("ix_user_skills_enabled"), "user_skills", ["enabled"])
    op.create_index(
        "ix_user_skills_owner_enabled",
        "user_skills",
        ["tenant_id", "user_id", "enabled"],
    )

    with op.batch_alter_table("agent_chat_sessions", schema=None) as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.String(length=36), nullable=True)
    with op.batch_alter_table("agent_chat_summaries", schema=None) as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.String(length=36), nullable=True)

    op.create_table(
        "personal_agent_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(length=600), nullable=False),
        sa.Column("media_url", sa.String(length=600), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"], ["agent_chat_messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_chat_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("tenant_id", "user_id", "session_id", "message_id"):
        op.create_index(
            op.f(f"ix_personal_agent_attachments_{column}"),
            "personal_agent_attachments",
            [column],
        )
    op.create_index(
        "ix_personal_agent_attachments_owner",
        "personal_agent_attachments",
        ["tenant_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personal_agent_attachments_owner", table_name="personal_agent_attachments"
    )
    for column in ("message_id", "session_id", "user_id", "tenant_id"):
        op.drop_index(
            op.f(f"ix_personal_agent_attachments_{column}"),
            table_name="personal_agent_attachments",
        )
    op.drop_table("personal_agent_attachments")

    with op.batch_alter_table("agent_chat_summaries", schema=None) as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.String(length=36), nullable=False)
    with op.batch_alter_table("agent_chat_sessions", schema=None) as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.String(length=36), nullable=False)

    op.drop_index("ix_user_skills_owner_enabled", table_name="user_skills")
    op.drop_index(op.f("ix_user_skills_enabled"), table_name="user_skills")
    op.drop_index(op.f("ix_user_skills_user_id"), table_name="user_skills")
    op.drop_index(op.f("ix_user_skills_tenant_id"), table_name="user_skills")
    op.drop_table("user_skills")
