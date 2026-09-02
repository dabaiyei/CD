"""add agent memory retrieval and conversation summaries

Revision ID: 4f5a8d9e1c20
Revises: b93e6d2f4a10
Create Date: 2026-08-31 23:55:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "4f5a8d9e1c20"
down_revision: str | Sequence[str] | None = "b93e6d2f4a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    embedding_type = Vector(384) if is_postgres else sa.JSON()
    with op.batch_alter_table("agent_memories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("embedding", embedding_type, nullable=True))
        batch_op.add_column(
            sa.Column(
                "embedding_model",
                sa.String(length=120),
                nullable=False,
                server_default="cineforge-hash-v1",
            )
        )
        batch_op.add_column(
            sa.Column("salience", sa.Float(), nullable=False, server_default="0.5")
        )
        batch_op.add_column(
            sa.Column("is_automatic", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("source_session_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("source_message_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("access_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_foreign_key(
            "fk_agent_memories_source_session_id_agent_chat_sessions",
            "agent_chat_sessions",
            ["source_session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(batch_op.f("ix_agent_memories_is_automatic"), ["is_automatic"])
        batch_op.create_index(
            batch_op.f("ix_agent_memories_source_session_id"), ["source_session_id"]
        )
        batch_op.create_index(
            batch_op.f("ix_agent_memories_source_message_id"), ["source_message_id"]
        )
        batch_op.create_index(
            "ix_agent_memories_retrieval_scope",
            ["tenant_id", "user_id", "project_id", "namespace"],
        )

    if is_postgres:
        op.create_index(
            "ix_agent_memories_embedding_hnsw",
            "agent_memories",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )

    op.create_table(
        "agent_chat_summaries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("through_message_id", sa.String(length=36), nullable=True),
        sa.Column("model_id", sa.String(length=36), nullable=True),
        sa.Column("source_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["ai_models.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_chat_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["through_message_id"], ["agent_chat_messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "version", name="uq_agent_chat_summary_version"),
    )
    for column in ("tenant_id", "user_id", "project_id", "session_id"):
        op.create_index(f"ix_agent_chat_summaries_{column}", "agent_chat_summaries", [column])
    op.create_index(
        "ix_agent_chat_summaries_session_created",
        "agent_chat_summaries",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_chat_summaries_session_created", table_name="agent_chat_summaries"
    )
    for column in ("session_id", "project_id", "user_id", "tenant_id"):
        op.drop_index(f"ix_agent_chat_summaries_{column}", table_name="agent_chat_summaries")
    op.drop_table("agent_chat_summaries")

    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_agent_memories_embedding_hnsw", table_name="agent_memories")
    with op.batch_alter_table("agent_memories", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_memories_retrieval_scope")
        batch_op.drop_index(batch_op.f("ix_agent_memories_source_message_id"))
        batch_op.drop_index(batch_op.f("ix_agent_memories_source_session_id"))
        batch_op.drop_index(batch_op.f("ix_agent_memories_is_automatic"))
        batch_op.drop_constraint(
            "fk_agent_memories_source_session_id_agent_chat_sessions", type_="foreignkey"
        )
        batch_op.drop_column("access_count")
        batch_op.drop_column("last_accessed_at")
        batch_op.drop_column("source_message_id")
        batch_op.drop_column("source_session_id")
        batch_op.drop_column("is_automatic")
        batch_op.drop_column("salience")
        batch_op.drop_column("embedding_model")
        batch_op.drop_column("embedding")
