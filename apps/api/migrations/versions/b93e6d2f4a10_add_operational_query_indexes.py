"""add operational task and notification indexes

Revision ID: b93e6d2f4a10
Revises: a8f47c2d19e6
Create Date: 2026-08-31 23:30:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b93e6d2f4a10"
down_revision: str | Sequence[str] | None = "a8f47c2d19e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_ai_tasks_project_type_status",
        "ai_tasks",
        ["project_id", "task_type", "status"],
        unique=False,
    )
    op.create_index(
        "ix_ai_tasks_tenant_user_created",
        "ai_tasks",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_tasks_tenant_created",
        "ai_tasks",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_tasks_status_lease",
        "ai_tasks",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_tenant_user_unread_created",
        "notifications",
        ["tenant_id", "user_id", "is_read", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_tenant_user_unread_created", table_name="notifications")
    op.drop_index("ix_ai_tasks_status_lease", table_name="ai_tasks")
    op.drop_index("ix_ai_tasks_tenant_created", table_name="ai_tasks")
    op.drop_index("ix_ai_tasks_tenant_user_created", table_name="ai_tasks")
    op.drop_index("ix_ai_tasks_project_type_status", table_name="ai_tasks")
