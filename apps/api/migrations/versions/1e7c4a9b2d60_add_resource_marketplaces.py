"""add resource marketplaces

Revision ID: 1e7c4a9b2d60
Revises: 6a2c8e4f1d90
Create Date: 2026-09-03 02:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1e7c4a9b2d60"
down_revision: str | Sequence[str] | None = "6a2c8e4f1d90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "user_id", "name", name="uq_user_template_name"),
    )
    op.create_index(op.f("ix_user_templates_category"), "user_templates", ["category"])
    op.create_index(op.f("ix_user_templates_tenant_id"), "user_templates", ["tenant_id"])
    op.create_index(op.f("ix_user_templates_user_id"), "user_templates", ["user_id"])
    op.create_index(
        "ix_user_templates_owner_updated",
        "user_templates",
        ["tenant_id", "user_id", "updated_at"],
    )

    op.create_table(
        "marketplace_listings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("resource_type", sa.String(length=8), nullable=False),
        sa.Column("publisher_tenant_id", sa.String(length=36), nullable=False),
        sa.Column("publisher_user_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("cover_url", sa.String(length=600), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("download_count", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["publisher_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_type",
            "publisher_user_id",
            "source_id",
            name="uq_marketplace_listing_source",
        ),
    )
    for column in (
        "resource_type",
        "publisher_tenant_id",
        "publisher_user_id",
        "source_id",
        "title",
        "category",
        "published",
    ):
        op.create_index(op.f(f"ix_marketplace_listings_{column}"), "marketplace_listings", [column])
    op.create_index(
        "ix_marketplace_listings_public_feed",
        "marketplace_listings",
        ["resource_type", "published", "updated_at"],
    )
    op.create_index(
        "ix_marketplace_listings_publisher",
        "marketplace_listings",
        ["publisher_user_id", "published"],
    )

    op.create_table(
        "marketplace_acquisitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("listing_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["listing_id"], ["marketplace_listings.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "user_id", name="uq_marketplace_acquisition_user"),
    )
    op.create_index(
        op.f("ix_marketplace_acquisitions_listing_id"),
        "marketplace_acquisitions",
        ["listing_id"],
    )
    op.create_index(
        op.f("ix_marketplace_acquisitions_target_id"),
        "marketplace_acquisitions",
        ["target_id"],
    )
    op.create_index(
        op.f("ix_marketplace_acquisitions_tenant_id"),
        "marketplace_acquisitions",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_marketplace_acquisitions_user_id"),
        "marketplace_acquisitions",
        ["user_id"],
    )
    op.create_index(
        "ix_marketplace_acquisitions_owner",
        "marketplace_acquisitions",
        ["tenant_id", "user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_marketplace_acquisitions_owner", table_name="marketplace_acquisitions")
    op.drop_index(op.f("ix_marketplace_acquisitions_user_id"), table_name="marketplace_acquisitions")
    op.drop_index(op.f("ix_marketplace_acquisitions_tenant_id"), table_name="marketplace_acquisitions")
    op.drop_index(op.f("ix_marketplace_acquisitions_target_id"), table_name="marketplace_acquisitions")
    op.drop_index(op.f("ix_marketplace_acquisitions_listing_id"), table_name="marketplace_acquisitions")
    op.drop_table("marketplace_acquisitions")

    op.drop_index("ix_marketplace_listings_publisher", table_name="marketplace_listings")
    op.drop_index("ix_marketplace_listings_public_feed", table_name="marketplace_listings")
    for column in (
        "published",
        "category",
        "title",
        "source_id",
        "publisher_user_id",
        "publisher_tenant_id",
        "resource_type",
    ):
        op.drop_index(op.f(f"ix_marketplace_listings_{column}"), table_name="marketplace_listings")
    op.drop_table("marketplace_listings")

    op.drop_index("ix_user_templates_owner_updated", table_name="user_templates")
    op.drop_index(op.f("ix_user_templates_user_id"), table_name="user_templates")
    op.drop_index(op.f("ix_user_templates_tenant_id"), table_name="user_templates")
    op.drop_index(op.f("ix_user_templates_category"), table_name="user_templates")
    op.drop_table("user_templates")
