"""Persist infinite canvas state and media per account."""

import sqlalchemy as sa
from alembic import op

revision = "f4c8a61d9032"
down_revision = "d3f5a7b9c120"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "canvas_storage_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("namespace", sa.String(100), nullable=False),
        sa.Column("key", sa.String(250), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.String(600), nullable=True),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", "namespace", "key", name="uq_canvas_storage_owner_key"),
    )
    op.create_index("ix_canvas_storage_items_tenant_id", "canvas_storage_items", ["tenant_id"])
    op.create_index("ix_canvas_storage_items_user_id", "canvas_storage_items", ["user_id"])


def downgrade():
    op.drop_table("canvas_storage_items")
