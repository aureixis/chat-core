"""Add enterprise isolation, worker controls, provenance, and tool governance.

Revision ID: 0003_enterprise_isolation
Revises: 0002_vector_search
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.database import Base

revision = "0003_enterprise_isolation"
down_revision = "0002_vector_search"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _index(table: str, name: str, columns: list[str], *, unique: bool = False) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns, unique=unique)


def _json_column(name: str, default: str) -> sa.Column:
    return sa.Column(name, sa.JSON(), nullable=False, server_default=sa.text(f"'{default}'"))


def _organization_column() -> sa.Column:
    return sa.Column(
        "organization_id",
        sa.Integer(),
        sa.ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )


def _backfill_organization(table: str, bot_column: str = "bot_id") -> None:
    if "organization_id" not in _columns(table):
        return
    op.execute(
        sa.text(
            f"UPDATE {table} SET organization_id = ("
            f"SELECT organization_id FROM bot_profiles WHERE bot_profiles.bot_id = {table}.{bot_column}"
            ") WHERE organization_id IS NULL"
        )
    )


def _enable_rls() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    admin = "COALESCE(current_setting('app.is_admin', true), 'false') = 'true'"
    worker = "COALESCE(current_setting('app.is_worker', true), 'false') = 'true'"
    user_match = "user_id::text = NULLIF(current_setting('app.user_id', true), '')"
    actor_match = "actor_id::text = NULLIF(current_setting('app.user_id', true), '')"
    reporter_match = "reporter_id::text = NULLIF(current_setting('app.user_id', true), '')"
    org_read = (
        "organization_id IS NOT NULL AND organization_id = ANY("
        "CASE WHEN COALESCE(current_setting('app.organization_ids', true), '') = '' "
        "THEN ARRAY[]::integer[] ELSE string_to_array("
        "current_setting('app.organization_ids', true), ',')::integer[] END)"
    )
    org_write = (
        "organization_id IS NOT NULL AND organization_id = ANY("
        "CASE WHEN COALESCE(current_setting('app.organization_write_ids', true), '') = '' "
        "THEN ARRAY[]::integer[] ELSE string_to_array("
        "current_setting('app.organization_write_ids', true), ',')::integer[] END)"
    )
    privileged_read = f"({admin} OR {worker} OR ({org_read}))"
    privileged_write = f"({admin} OR {worker} OR ({org_write}))"
    organization_record_read = (
        "id = ANY(CASE WHEN COALESCE(current_setting('app.organization_ids', true), '') = '' "
        "THEN ARRAY[]::integer[] ELSE string_to_array("
        "current_setting('app.organization_ids', true), ',')::integer[] END)"
    )
    organization_record_write = (
        "id = ANY(CASE WHEN COALESCE(current_setting('app.organization_write_ids', true), '') = '' "
        "THEN ARRAY[]::integer[] ELSE string_to_array("
        "current_setting('app.organization_write_ids', true), ',')::integer[] END)"
    )
    current_user = "NULLIF(current_setting('app.user_id', true), '')"
    action_org_matches = (
        "bot_actions.organization_id IS NOT DISTINCT FROM (SELECT profile.organization_id "
        "FROM bot_profiles AS profile WHERE profile.bot_id = bot_actions.bot_id)"
    )
    human_action = (
        f"(({action_org_matches}) AND ("
        "(bot_actions.action_type = 'evaluate_connection' AND EXISTS (SELECT 1 FROM connections AS c "
        f"WHERE c.id = bot_actions.target_id AND c.requester_id::text = {current_user} "
        "AND c.recipient_id = bot_actions.bot_id)) OR "
        "(bot_actions.action_type = 'evaluate_companion' AND EXISTS (SELECT 1 FROM companions AS cp "
        f"WHERE cp.id = bot_actions.target_id AND cp.user_id::text = {current_user} "
        "AND cp.bot_id = bot_actions.bot_id)) OR "
        "(bot_actions.action_type = 'reply_message' AND EXISTS (SELECT 1 FROM messages AS m "
        "JOIN conversations AS cv ON cv.id = m.conversation_id "
        f"WHERE m.id = bot_actions.target_id AND m.sender_id::text = {current_user} "
        "AND (cv.user_a_id = bot_actions.bot_id OR cv.user_b_id = bot_actions.bot_id))) OR "
        "(bot_actions.action_type = 'embed_memory' AND EXISTS (SELECT 1 FROM robot_memories AS rm "
        f"WHERE rm.id = bot_actions.target_id AND rm.user_id::text = {current_user} "
        "AND rm.bot_id = bot_actions.bot_id))"
        "))"
    )

    policies: dict[str, list[str]] = {
        "organizations": [
            f"CREATE POLICY lamya_organization_read ON organizations FOR SELECT USING ({admin} OR {worker} OR ({organization_record_read}))",
            f"CREATE POLICY lamya_organization_write ON organizations FOR ALL USING ({admin} OR {worker} OR ({organization_record_write})) WITH CHECK ({admin} OR {worker} OR ({organization_record_write}))",
        ],
        "organization_memberships": [
            f"CREATE POLICY lamya_membership_read ON organization_memberships FOR SELECT USING ({admin} OR {worker} OR user_id::text = {current_user} OR ({org_read}))",
            f"CREATE POLICY lamya_membership_write ON organization_memberships FOR ALL USING ({admin} OR {worker} OR ({org_write})) WITH CHECK ({admin} OR {worker} OR ({org_write}))",
        ],
        "bot_profiles": [
            "CREATE POLICY lamya_public_robot_profiles ON bot_profiles FOR SELECT USING (true)",
            f"CREATE POLICY lamya_manage_robot_profiles ON bot_profiles FOR ALL USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "persona_versions": [
            f"CREATE POLICY lamya_persona_read ON persona_versions FOR SELECT USING {privileged_read}",
            f"CREATE POLICY lamya_persona_write ON persona_versions FOR ALL USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "robot_memories": [
            f"CREATE POLICY lamya_memory_read ON robot_memories FOR SELECT USING ({admin} OR {worker} OR {user_match} OR ({org_read}))",
            f"CREATE POLICY lamya_memory_write ON robot_memories FOR ALL USING ({admin} OR {worker} OR {user_match} OR ({org_write})) WITH CHECK ({admin} OR {worker} OR {user_match} OR ({org_write}))",
        ],
        "robot_knowledge_sources": [
            f"CREATE POLICY lamya_knowledge_read ON robot_knowledge_sources FOR SELECT USING {privileged_read}",
            f"CREATE POLICY lamya_knowledge_write ON robot_knowledge_sources FOR ALL USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "knowledge_chunks": [
            f"CREATE POLICY lamya_chunk_read ON knowledge_chunks FOR SELECT USING {privileged_read}",
            f"CREATE POLICY lamya_chunk_write ON knowledge_chunks FOR ALL USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "bot_actions": [
            f"CREATE POLICY lamya_action_read ON bot_actions FOR SELECT USING {privileged_read}",
            f"CREATE POLICY lamya_action_insert ON bot_actions FOR INSERT WITH CHECK ({admin} OR {worker} OR ({org_write}) OR {human_action})",
            f"CREATE POLICY lamya_action_modify ON bot_actions FOR UPDATE USING {privileged_write} WITH CHECK {privileged_write}",
            f"CREATE POLICY lamya_action_delete ON bot_actions FOR DELETE USING {privileged_write}",
        ],
        "usage_ledger": [
            f"CREATE POLICY lamya_usage_read ON usage_ledger FOR SELECT USING {privileged_read}",
            f"CREATE POLICY lamya_usage_write ON usage_ledger FOR ALL USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "moderation_cases": [
            f"CREATE POLICY lamya_moderation_read ON moderation_cases FOR SELECT USING ({admin} OR {worker} OR {reporter_match} OR ({org_read}))",
            f"CREATE POLICY lamya_moderation_insert ON moderation_cases FOR INSERT WITH CHECK ({admin} OR {worker} OR {reporter_match} OR ({org_write}))",
            f"CREATE POLICY lamya_moderation_modify ON moderation_cases FOR UPDATE USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "audit_events": [
            f"CREATE POLICY lamya_audit_read ON audit_events FOR SELECT USING ({admin} OR {worker} OR {actor_match} OR ({org_read}))",
            f"CREATE POLICY lamya_audit_append ON audit_events FOR INSERT WITH CHECK ({admin} OR {worker} OR {actor_match} OR actor_id IS NULL)",
        ],
        "audit_outbox": [
            f"CREATE POLICY lamya_audit_outbox_read ON audit_outbox FOR SELECT USING ({admin} OR {worker})",
            f"CREATE POLICY lamya_audit_outbox_insert ON audit_outbox FOR INSERT WITH CHECK ({admin} OR {worker} OR EXISTS (SELECT 1 FROM audit_events AS ae WHERE ae.id = event_id AND ae.actor_id::text = {current_user}))",
            f"CREATE POLICY lamya_audit_outbox_modify ON audit_outbox FOR UPDATE USING ({admin} OR {worker}) WITH CHECK ({admin} OR {worker})",
        ],
        "robot_tool_grants": [
            f"CREATE POLICY lamya_tool_grant_read ON robot_tool_grants FOR SELECT USING {privileged_read}",
            f"CREATE POLICY lamya_tool_grant_write ON robot_tool_grants FOR ALL USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "tool_invocations": [
            f"CREATE POLICY lamya_tool_invocation_read ON tool_invocations FOR SELECT USING {privileged_read}",
            f"CREATE POLICY lamya_tool_invocation_write ON tool_invocations FOR ALL USING {privileged_write} WITH CHECK {privileged_write}",
        ],
        "tool_definitions": [
            f"CREATE POLICY lamya_tool_definition_scope ON tool_definitions FOR ALL USING ({admin} OR {worker}) WITH CHECK ({admin} OR {worker})"
        ],
        "platform_state": [
            "CREATE POLICY lamya_platform_state_read ON platform_state FOR SELECT USING (true)",
            f"CREATE POLICY lamya_platform_state_write ON platform_state FOR ALL USING ({admin} OR {worker}) WITH CHECK ({admin} OR {worker})",
        ],
    }
    for table, statements in policies.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        for statement in statements:
            op.execute(statement)


def upgrade() -> None:
    _add("ai_settings", sa.Column("api_key_ref", sa.String(500), nullable=True))
    _add("translation_settings", sa.Column("api_key_ref", sa.String(500), nullable=True))
    for table in ("posts", "comments", "messages"):
        _add(table, _json_column("generation_metadata", "{}"))

    for table in (
        "persona_versions",
        "robot_memories",
        "bot_actions",
        "robot_knowledge_sources",
        "knowledge_chunks",
        "moderation_cases",
        "audit_events",
        "usage_ledger",
    ):
        _add(table, _organization_column())
        _index(table, f"ix_{table}_organization_id", ["organization_id"])

    _add("bot_actions", _json_column("retrieval_context", "[]"))
    _add(
        "bot_actions",
        sa.Column("fleet_generation", sa.Integer(), nullable=False, server_default="1"),
    )
    _add("bot_actions", sa.Column("priority", sa.Integer(), nullable=False, server_default="50"))
    _add("bot_actions", sa.Column("locked_by", sa.String(160), nullable=True))
    _add("bot_actions", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    _index("bot_actions", "ix_bot_actions_priority", ["priority"])
    _index("bot_actions", "ix_bot_actions_locked_by", ["locked_by"])

    _add(
        "robot_knowledge_sources",
        sa.Column("trust_level", sa.String(30), nullable=False, server_default="reviewed"),
    )
    _add("robot_knowledge_sources", _json_column("risk_labels", "[]"))
    _add("audit_events", sa.Column("previous_hash", sa.String(64), nullable=True))
    _add("audit_events", sa.Column("event_hash", sa.String(64), nullable=True))
    _add("audit_events", sa.Column("canonical_payload", sa.Text(), nullable=True))
    _index("audit_events", "uq_audit_events_event_hash", ["event_hash"], unique=True)

    for table in (
        "platform_state",
        "audit_chain_heads",
        "audit_outbox",
        "tool_definitions",
        "robot_tool_grants",
        "tool_invocations",
    ):
        Base.metadata.tables[table].create(bind=op.get_bind(), checkfirst=True)

    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT COUNT(*) FROM platform_state WHERE id = 1")) == 0:
        bind.execute(
            sa.text(
                "INSERT INTO platform_state (id, fleet_generation, fleet_paused) VALUES (1, 1, false)"
            )
        )
    if bind.scalar(sa.text("SELECT COUNT(*) FROM audit_chain_heads WHERE id = 1")) == 0:
        bind.execute(
            sa.text("INSERT INTO audit_chain_heads (id, last_hash) VALUES (1, :hash)"),
            {"hash": "0" * 64},
        )

    for table in (
        "persona_versions",
        "robot_memories",
        "bot_actions",
        "robot_knowledge_sources",
        "knowledge_chunks",
        "usage_ledger",
    ):
        _backfill_organization(table)
    op.execute(
        sa.text(
            "UPDATE users SET bot_prompt = NULL, bot_ideology = NULL "
            "WHERE is_bot = true AND EXISTS ("
            "SELECT 1 FROM persona_versions WHERE persona_versions.bot_id = users.id)"
        )
    )
    _enable_rls()


def downgrade() -> None:
    # Isolation and audit upgrades are intentionally non-destructive. Restore a
    # pre-upgrade backup if a full rollback is required.
    pass
