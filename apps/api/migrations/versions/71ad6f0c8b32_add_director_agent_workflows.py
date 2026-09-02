"""add persisted director agent workflows

Revision ID: 71ad6f0c8b32
Revises: 4f5a8d9e1c20
Create Date: 2026-08-31 23:59:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "71ad6f0c8b32"
down_revision: str | Sequence[str] | None = "4f5a8d9e1c20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "director_workflow_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("chat_session_id", sa.String(length=36), nullable=True),
        sa.Column(
            "stage",
            sa.Enum(
                "SCRIPT_ADAPTING", "SCRIPT_REVIEWING", "AWAITING_SCRIPT_DECISION",
                "SCRIPT_REPAIRING", "ASSET_EXTRACTING", "READY_FOR_ASSET_IMAGES",
                "ASSET_PREPARING", "STORYBOARD_GENERATING", "STORYBOARD_REVIEWING",
                "AWAITING_STORYBOARD_DECISION", "STORYBOARD_REPAIRING", "READY_FOR_VIDEO",
                "FAILED", "CANCELLED", name="directorworkflowstage", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING", "WAITING_USER", "COMPLETED", "FAILED", "CANCELLED",
                name="directorworkflowstatus", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("script_version_id", sa.String(length=36), nullable=True),
        sa.Column("asset_extraction_id", sa.String(length=36), nullable=True),
        sa.Column("storyboard_version_id", sa.String(length=36), nullable=True),
        sa.Column("current_task_id", sa.String(length=36), nullable=True),
        sa.Column("context_snapshot", sa.JSON(), nullable=False),
        sa.Column("last_message", sa.String(length=1000), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_extraction_id"], ["asset_extractions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chat_session_id"], ["agent_chat_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["current_task_id"], ["ai_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["script_version_id"], ["script_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["storyboard_version_id"], ["storyboard_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "tenant_id", "user_id", "project_id", "chapter_id", "chat_session_id", "stage", "status",
        "script_version_id", "asset_extraction_id", "storyboard_version_id", "current_task_id",
    ):
        op.create_index(f"ix_director_workflow_runs_{column}", "director_workflow_runs", [column])
    op.create_index(
        "ix_director_workflows_chapter_updated", "director_workflow_runs", ["chapter_id", "updated_at"]
    )
    op.create_index(
        "ix_director_workflows_project_status", "director_workflow_runs", ["project_id", "status"]
    )

    op.create_table(
        "director_child_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("parent_child_run_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED",
                name="directorchildstatus", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("input_refs", sa.JSON(), nullable=False),
        sa.Column("output_refs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_child_run_id"], ["director_child_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["ai_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["director_workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "tenant_id",
        "user_id",
        "project_id",
        "workflow_id",
        "parent_child_run_id",
        "kind",
        "status",
    ):
        op.create_index(f"ix_director_child_runs_{column}", "director_child_runs", [column])
    op.create_index(
        "ix_director_child_runs_task_id", "director_child_runs", ["task_id"], unique=True
    )
    op.create_index(
        "ix_director_child_workflow_created", "director_child_runs", ["workflow_id", "created_at"]
    )

    op.create_table(
        "director_decision_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("child_run_id", sa.String(length=36), nullable=True),
        sa.Column("decision_type", sa.String(length=80), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("selected_option", sa.String(length=80), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["child_run_id"], ["director_child_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["director_workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "tenant_id", "user_id", "project_id", "workflow_id", "child_run_id", "decision_type", "resolved",
    ):
        op.create_index(f"ix_director_decision_requests_{column}", "director_decision_requests", [column])
    op.create_index(
        "ix_director_decisions_workflow_created", "director_decision_requests", ["workflow_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("director_decision_requests")
    op.drop_table("director_child_runs")
    op.drop_table("director_workflow_runs")
