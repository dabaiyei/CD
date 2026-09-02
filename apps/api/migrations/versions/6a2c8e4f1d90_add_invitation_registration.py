"""add invitation registration

Revision ID: 6a2c8e4f1d90
Revises: 3f9a6d1c2b70
Create Date: 2026-09-03 15:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6a2c8e4f1d90"
down_revision: str | Sequence[str] | None = "3f9a6d1c2b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("invite_url_prefix", sa.String(length=500), nullable=True))

    op.create_table(
        "invitation_codes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("max_registrations", sa.Integer(), nullable=False),
        sa.Column("registration_count", sa.Integer(), nullable=False),
        sa.Column("initial_credits", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(op.f("ix_invitation_codes_code"), "invitation_codes", ["code"], unique=True)
    op.create_index(
        op.f("ix_invitation_codes_created_by_id"),
        "invitation_codes",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(op.f("ix_invitation_codes_deleted_at"), "invitation_codes", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_invitation_codes_enabled"), "invitation_codes", ["enabled"], unique=False)
    op.create_index(op.f("ix_invitation_codes_tenant_id"), "invitation_codes", ["tenant_id"], unique=False)
    op.create_index(
        "ix_invitation_codes_tenant_active",
        "invitation_codes",
        ["tenant_id", "enabled", "deleted_at"],
        unique=False,
    )
    op.create_index(
        "ix_invitation_codes_tenant_created",
        "invitation_codes",
        ["tenant_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "invitation_redemptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("invitation_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("initial_credits", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invitation_id"], ["invitation_codes.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_invitation_redemption_user"),
    )
    op.create_index(
        op.f("ix_invitation_redemptions_invitation_id"),
        "invitation_redemptions",
        ["invitation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_invitation_redemptions_tenant_id"),
        "invitation_redemptions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_invitation_redemptions_user_id"),
        "invitation_redemptions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_invitation_redemptions_invitation_created",
        "invitation_redemptions",
        ["invitation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_invitation_redemptions_invitation_created", table_name="invitation_redemptions")
    op.drop_index(op.f("ix_invitation_redemptions_user_id"), table_name="invitation_redemptions")
    op.drop_index(op.f("ix_invitation_redemptions_tenant_id"), table_name="invitation_redemptions")
    op.drop_index(op.f("ix_invitation_redemptions_invitation_id"), table_name="invitation_redemptions")
    op.drop_table("invitation_redemptions")

    op.drop_index("ix_invitation_codes_tenant_created", table_name="invitation_codes")
    op.drop_index("ix_invitation_codes_tenant_active", table_name="invitation_codes")
    op.drop_index(op.f("ix_invitation_codes_tenant_id"), table_name="invitation_codes")
    op.drop_index(op.f("ix_invitation_codes_enabled"), table_name="invitation_codes")
    op.drop_index(op.f("ix_invitation_codes_deleted_at"), table_name="invitation_codes")
    op.drop_index(op.f("ix_invitation_codes_created_by_id"), table_name="invitation_codes")
    op.drop_index(op.f("ix_invitation_codes_code"), table_name="invitation_codes")
    op.drop_table("invitation_codes")

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("invite_url_prefix")
