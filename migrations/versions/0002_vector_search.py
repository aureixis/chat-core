"""Add pgvector-backed memory and knowledge retrieval.

Revision ID: 0002_vector_search
Revises: 0001_lamya_enterprise
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

from app.vector_config import VECTOR_DIMENSIONS

revision = "0002_vector_search"
down_revision = "0001_lamya_enterprise"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"
    if is_postgresql:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    _add_column_if_missing(
        "ai_settings", sa.Column("embedding_model", sa.String(120), nullable=True)
    )
    _add_column_if_missing(
        "ai_settings",
        sa.Column(
            "embedding_dimensions",
            sa.Integer(),
            nullable=False,
            server_default=str(VECTOR_DIMENSIONS),
        ),
    )
    _add_column_if_missing(
        "ai_settings",
        sa.Column("embeddings_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    _add_column_if_missing(
        "robot_memories",
        sa.Column("embedding", VECTOR(VECTOR_DIMENSIONS), nullable=True),
    )
    _add_column_if_missing(
        "robot_memories", sa.Column("embedding_model", sa.String(120), nullable=True)
    )
    _add_column_if_missing(
        "robot_memories",
        sa.Column("embedding_status", sa.String(30), nullable=False, server_default="pending"),
    )
    _add_column_if_missing(
        "robot_memories", sa.Column("content_hash", sa.String(64), nullable=True)
    )
    _add_column_if_missing(
        "robot_memories", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True)
    )

    if "knowledge_chunks" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "source_id",
                sa.Integer(),
                sa.ForeignKey("robot_knowledge_sources.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "bot_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("embedding", VECTOR(VECTOR_DIMENSIONS), nullable=True),
            sa.Column("embedding_model", sa.String(120), nullable=True),
            sa.Column("embedding_status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("source_id", "chunk_index", name="uq_knowledge_chunk_position"),
        )

    if "ix_robot_memories_embedding_status" not in _indexes("robot_memories"):
        op.create_index(
            "ix_robot_memories_embedding_status", "robot_memories", ["embedding_status"]
        )
    if "ix_knowledge_chunks_source_id" not in _indexes("knowledge_chunks"):
        op.create_index("ix_knowledge_chunks_source_id", "knowledge_chunks", ["source_id"])
    if "ix_knowledge_chunks_bot_id" not in _indexes("knowledge_chunks"):
        op.create_index("ix_knowledge_chunks_bot_id", "knowledge_chunks", ["bot_id"])
    if "ix_knowledge_chunks_embedding_status" not in _indexes("knowledge_chunks"):
        op.create_index(
            "ix_knowledge_chunks_embedding_status", "knowledge_chunks", ["embedding_status"]
        )
    if "ix_knowledge_chunk_robot_status" not in _indexes("knowledge_chunks"):
        op.create_index(
            "ix_knowledge_chunk_robot_status",
            "knowledge_chunks",
            ["bot_id", "embedding_status"],
        )

    bind.execute(
        sa.text(
            "UPDATE robot_knowledge_sources SET status = 'pending' "
            "WHERE NOT EXISTS (SELECT 1 FROM knowledge_chunks "
            "WHERE knowledge_chunks.source_id = robot_knowledge_sources.id)"
        )
    )

    if is_postgresql:
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_robot_memories_embedding_hnsw "
            "ON robot_memories USING hnsw (embedding vector_cosine_ops) "
            "WHERE embedding IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_hnsw "
            "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
            "WHERE embedding IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw")
        op.execute("DROP INDEX IF EXISTS ix_robot_memories_embedding_hnsw")
    if "ix_robot_memories_embedding_status" in _indexes("robot_memories"):
        op.drop_index("ix_robot_memories_embedding_status", table_name="robot_memories")
    if "knowledge_chunks" in sa.inspect(bind).get_table_names():
        op.drop_table("knowledge_chunks")
    for name in (
        "embedded_at",
        "content_hash",
        "embedding_status",
        "embedding_model",
        "embedding",
    ):
        if name in _columns("robot_memories"):
            op.drop_column("robot_memories", name)
    for name in ("embeddings_enabled", "embedding_dimensions", "embedding_model"):
        if name in _columns("ai_settings"):
            op.drop_column("ai_settings", name)
