from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Companion,
    Connection,
    Conversation,
    Message,
    OrganizationMembership,
    Post,
    RobotMemory,
    User,
)
from ..schemas import AccountDeleteRequest
from ..security import current_user, verify_password
from ..services.audit import record_audit

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


@router.get("/export")
def export_my_data(user: User = Depends(current_user), database: Session = Depends(get_db)):
    connection_rows = list(
        database.scalars(
            select(Connection).where(
                or_(Connection.requester_id == user.id, Connection.recipient_id == user.id)
            )
        )
    )
    conversation_rows = list(
        database.scalars(
            select(Conversation).where(
                or_(Conversation.user_a_id == user.id, Conversation.user_b_id == user.id)
            )
        )
    )
    conversation_ids = [row.id for row in conversation_rows]
    message_rows = (
        list(
            database.scalars(
                select(Message)
                .where(Message.conversation_id.in_(conversation_ids))
                .order_by(Message.created_at)
            )
        )
        if conversation_ids
        else []
    )
    companion_rows = list(database.scalars(select(Companion).where(Companion.user_id == user.id)))
    memory_rows = list(database.scalars(select(RobotMemory).where(RobotMemory.user_id == user.id)))
    post_rows = list(database.scalars(select(Post).where(Post.author_id == user.id)))
    record_audit(database, "privacy.exported", "user", user.id, actor_id=user.id)
    database.commit()
    return {
        "profile": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "bio": user.bio,
            "preferred_language": user.preferred_language,
            "created_at": user.created_at,
        },
        "connections": [
            {
                "id": row.id,
                "requester_id": row.requester_id,
                "recipient_id": row.recipient_id,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in connection_rows
        ],
        "conversations": [
            {"id": row.id, "user_a_id": row.user_a_id, "user_b_id": row.user_b_id}
            for row in conversation_rows
        ],
        "messages": [
            {
                "id": row.id,
                "conversation_id": row.conversation_id,
                "sender_id": row.sender_id,
                "text": row.original_text,
                "created_at": row.created_at,
            }
            for row in message_rows
        ],
        "companions": [
            {"id": row.id, "bot_id": row.bot_id, "status": row.status} for row in companion_rows
        ],
        "memories": [
            {"id": row.id, "bot_id": row.bot_id, "kind": row.kind, "content": row.content}
            for row in memory_rows
        ],
        "posts": [
            {
                "id": row.id,
                "content": row.content,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in post_rows
        ],
    }


@router.delete("/account", status_code=204)
def delete_my_account(
    payload: AccountDeleteRequest,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    if user.is_admin:
        raise HTTPException(
            status_code=409,
            detail="Administrator accounts must be removed by another administrator",
        )
    if user.is_guest or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Password confirmation failed")
    owned_organizations = list(
        database.scalars(
            select(OrganizationMembership.organization_id).where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.role == "owner",
            )
        )
    )
    for organization_id in owned_organizations:
        owner_count = database.scalar(
            select(func.count(OrganizationMembership.id)).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.role == "owner",
            )
        )
        if (owner_count or 0) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Transfer organization ownership before deleting this account",
            )
    record_audit(database, "privacy.account.deleted", "user", user.id, actor_id=user.id)
    database.delete(user)
    database.commit()
    return Response(status_code=204)
