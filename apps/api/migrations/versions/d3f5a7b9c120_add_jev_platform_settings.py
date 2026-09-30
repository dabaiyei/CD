"""Persist JEV platform selection and separately encrypted credentials."""

import sqlalchemy as sa
from alembic import op

revision = "d3f5a7b9c120"
down_revision = "c83a52d901ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier development installs already created the legacy configuration table.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "jev_configurations" not in tables:
        op.create_table(
            "jev_configurations",
            sa.Column(
                "tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("encrypted_api_key", sa.Text(), nullable=True),
            sa.Column("model", sa.String(120), nullable=False),
            sa.Column("timeout_seconds", sa.Float(), nullable=False),
            sa.Column("route_confidence", sa.Float(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "jev_platform_settings" not in tables:
        op.create_table(
            "jev_platform_settings",
            sa.Column(
                "tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
            ),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("profiles", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("jev_platform_settings")
    # Keep the original Typesafe settings usable by the previous application.
