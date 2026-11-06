"""Persist expiring and revocable video-chat invitations."""

import sqlalchemy as sa
from alembic import op

revision = "0004_video_chat_invitations"
down_revision = "0003_enterprise_isolation"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "video_chat_invitations" not in inspector.get_table_names():
        op.create_table(
            "video_chat_invitations",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("label", sa.String(120), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {
        row["name"] for row in sa.inspect(op.get_bind()).get_indexes("video_chat_invitations")
    }
    if "ix_video_chat_invitations_created_at" not in indexes:
        op.create_index(
            "ix_video_chat_invitations_created_at", "video_chat_invitations", ["created_at"]
        )


def downgrade():
    op.drop_table("video_chat_invitations")
