from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .models import Companion, Connection, Conversation, Message, Post, User


def clear_expired_guests(database: Session) -> int:
    expired_ids = list(
        database.scalars(
            select(User.id).where(
                User.is_guest.is_(True), User.guest_expires_at <= datetime.now(timezone.utc)
            )
        )
    )
    if not expired_ids:
        return 0
    conversation_ids = list(
        database.scalars(
            select(Conversation.id).where(
                or_(
                    Conversation.user_a_id.in_(expired_ids), Conversation.user_b_id.in_(expired_ids)
                )
            )
        )
    )
    if conversation_ids:
        database.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        database.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
    database.execute(
        delete(Connection).where(
            or_(Connection.requester_id.in_(expired_ids), Connection.recipient_id.in_(expired_ids))
        )
    )
    database.execute(delete(Companion).where(Companion.user_id.in_(expired_ids)))
    database.execute(delete(Post).where(Post.author_id.in_(expired_ids)))
    database.execute(delete(User).where(User.id.in_(expired_ids)))
    database.commit()
    return len(expired_ids)
