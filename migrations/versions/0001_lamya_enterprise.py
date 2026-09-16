"""Lamya enterprise baseline with legacy schema compatibility.

Revision ID: 0001_lamya_enterprise
Revises:
Create Date: 2026-09-16
"""

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.database import Base

revision = "0001_lamya_enterprise"
down_revision = None
branch_labels = None
depends_on = None


def _add_missing_columns(table: str, columns: Iterable[sa.Column]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def _create_index(table: str, name: str, columns: list[str], *, unique: bool = False) -> None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return
    existing = {index["name"] for index in inspector.get_indexes(table)}
    existing_columns = {
        tuple(index.get("column_names") or []) for index in inspector.get_indexes(table)
    }
    if unique:
        existing_columns.update(
            tuple(constraint.get("column_names") or [])
            for constraint in inspector.get_unique_constraints(table)
        )
    if name not in existing and tuple(columns) not in existing_columns:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    # This first revision adopts existing Lamya installations as well as creating fresh databases.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=op.get_bind())

    _add_missing_columns(
        "users",
        [
            sa.Column("bio", sa.Text(), nullable=True),
            sa.Column("profile_picture_url", sa.String(1000), nullable=True),
            sa.Column("sex", sa.String(30), nullable=True),
            sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("guest_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("bot_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("bot_prompt", sa.Text(), nullable=True),
            sa.Column("bot_ideology", sa.Text(), nullable=True),
            sa.Column(
                "bot_post_interval_minutes", sa.Integer(), nullable=False, server_default="1440"
            ),
            sa.Column("bot_autonomous", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("account_status", sa.String(30), nullable=False, server_default="active"),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
        ],
    )
    _add_missing_columns(
        "connections",
        [
            sa.Column("decision_reason", sa.Text(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
        ],
    )
    _add_missing_columns(
        "conversations",
        [
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
        ],
    )
    _add_missing_columns(
        "messages",
        [
            sa.Column("client_message_id", sa.String(100), nullable=True),
            sa.Column("in_reply_to_id", sa.Integer(), nullable=True),
            sa.Column("is_bot_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "moderation_status", sa.String(30), nullable=False, server_default="approved"
            ),
            sa.Column("agent_chain_depth", sa.Integer(), nullable=False, server_default="0"),
        ],
    )
    _add_missing_columns(
        "translation_settings", [sa.Column("system_prompt", sa.Text(), nullable=True)]
    )
    _add_missing_columns(
        "posts",
        [
            sa.Column("visibility", sa.String(30), nullable=False, server_default="public"),
            sa.Column("status", sa.String(30), nullable=False, server_default="published"),
            sa.Column(
                "moderation_status", sa.String(30), nullable=False, server_default="approved"
            ),
            sa.Column("correlation_id", sa.String(100), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
        ],
    )
    _add_missing_columns(
        "comments",
        [
            sa.Column("parent_comment_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="published"),
            sa.Column(
                "moderation_status", sa.String(30), nullable=False, server_default="approved"
            ),
        ],
    )
    _add_missing_columns(
        "companions",
        [
            sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("decision_reason", sa.Text(), nullable=True),
            sa.Column("last_interaction_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
        ],
    )
    _add_missing_columns(
        "bot_actions",
        [
            sa.Column("trigger", sa.String(40), nullable=False, server_default="system"),
            sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("policy_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("idempotency_key", sa.String(160), nullable=True),
            sa.Column(
                "scheduled_for",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        ],
    )
    bind = op.get_bind()
    if "bot_actions" in sa.inspect(bind).get_table_names():
        bind.execute(
            sa.text(
                "UPDATE bot_actions SET idempotency_key = 'legacy:' || id WHERE idempotency_key IS NULL"
            )
        )
    _create_index("users", "ix_users_is_bot", ["is_bot"])
    _create_index("users", "ix_users_account_status", ["account_status"])
    _create_index("connections", "ix_connections_status", ["status"])
    _create_index("messages", "ix_messages_in_reply_to_id", ["in_reply_to_id"])
    _create_index("messages", "ix_messages_moderation_status", ["moderation_status"])
    _create_index("posts", "ix_posts_status", ["status"])
    _create_index("posts", "ix_posts_moderation_status", ["moderation_status"])
    _create_index("bot_actions", "ix_bot_action_queue", ["status", "scheduled_for"])
    _create_index("bot_actions", "uq_bot_actions_idempotency_key", ["idempotency_key"], unique=True)
    _create_index("posts", "uq_posts_correlation_id", ["correlation_id"], unique=True)


def downgrade() -> None:
    # The adoption revision is intentionally non-destructive. Restore from a backup to roll back.
    pass
