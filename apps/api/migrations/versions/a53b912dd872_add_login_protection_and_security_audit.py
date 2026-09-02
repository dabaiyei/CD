"""add login protection and security audit

Revision ID: a53b912dd872
Revises: f17c9d4e2b61
Create Date: 2026-08-31 19:20:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a53b912dd872"
down_revision: str | Sequence[str] | None = "f17c9d4e2b61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_login_guards",
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("identity_hash"),
    )
    op.create_index(op.f("ix_auth_login_guards_tenant_id"), "auth_login_guards", ["tenant_id"])
    op.create_index(op.f("ix_auth_login_guards_user_id"), "auth_login_guards", ["user_id"])
    op.create_index(
        op.f("ix_auth_login_guards_locked_until"), "auth_login_guards", ["locked_until"]
    )
    op.create_table(
        "security_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_security_events_tenant_id"), "security_events", ["tenant_id"])
    op.create_index(op.f("ix_security_events_user_id"), "security_events", ["user_id"])
    op.create_index(op.f("ix_security_events_event_type"), "security_events", ["event_type"])
    op.create_index(op.f("ix_security_events_success"), "security_events", ["success"])
    op.create_index(op.f("ix_security_events_subject_hash"), "security_events", ["subject_hash"])
    op.create_index(op.f("ix_security_events_ip_hash"), "security_events", ["ip_hash"])
    op.create_index(op.f("ix_security_events_created_at"), "security_events", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_security_events_created_at"), table_name="security_events")
    op.drop_index(op.f("ix_security_events_ip_hash"), table_name="security_events")
    op.drop_index(op.f("ix_security_events_subject_hash"), table_name="security_events")
    op.drop_index(op.f("ix_security_events_success"), table_name="security_events")
    op.drop_index(op.f("ix_security_events_event_type"), table_name="security_events")
    op.drop_index(op.f("ix_security_events_user_id"), table_name="security_events")
    op.drop_index(op.f("ix_security_events_tenant_id"), table_name="security_events")
    op.drop_table("security_events")
    op.drop_index(op.f("ix_auth_login_guards_locked_until"), table_name="auth_login_guards")
    op.drop_index(op.f("ix_auth_login_guards_user_id"), table_name="auth_login_guards")
    op.drop_index(op.f("ix_auth_login_guards_tenant_id"), table_name="auth_login_guards")
    op.drop_table("auth_login_guards")
