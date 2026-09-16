from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import BotProfile, Connection, User
from ..schemas import ConnectionCreate, ConnectionResponse
from ..security import current_user
from ..services.agents import enqueue_action, ensure_conversation
from ..services.audit import record_audit

router = APIRouter(prefix="/api/connections", tags=["connections"])


def _between(first_id: int, second_id: int):
    return or_(
        (Connection.requester_id == first_id) & (Connection.recipient_id == second_id),
        (Connection.requester_id == second_id) & (Connection.recipient_id == first_id),
    )


@router.get("", response_model=list[ConnectionResponse])
def connections(user: User = Depends(current_user), database: Session = Depends(get_db)):
    return list(
        database.scalars(
            select(Connection)
            .where(or_(Connection.requester_id == user.id, Connection.recipient_id == user.id))
            .order_by(Connection.created_at.desc())
        )
    )


@router.post("", response_model=ConnectionResponse, status_code=201)
def create_connection(
    payload: ConnectionCreate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    recipient = database.get(User, payload.recipient_id)
    if payload.recipient_id == user.id or not recipient or recipient.account_status != "active":
        raise HTTPException(status_code=404, detail="Recipient not found")
    if database.scalar(select(Connection).where(_between(user.id, recipient.id))):
        raise HTTPException(status_code=409, detail="A connection already exists")
    row = Connection(
        requester_id=user.id,
        recipient_id=recipient.id,
        status="evaluating" if recipient.is_bot else "pending",
    )
    database.add(row)
    database.flush()
    if recipient.is_bot:
        profile = database.get(BotProfile, recipient.id)
        if not profile or profile.status != "active" or not profile.connections_auto_decide:
            row.status = "pending"
        else:
            enqueue_action(
                database,
                recipient.id,
                "evaluate_connection",
                target_id=row.id,
                trigger="connection_request",
                idempotency_key=f"connection-eval:{row.id}",
            )
    record_audit(
        database, "connection.requested", "connection", row.id, actor_id=user.id, actor_type="user"
    )
    database.commit()
    database.refresh(row)
    return row


@router.patch("/{connection_id}", response_model=ConnectionResponse)
def update_connection(
    connection_id: int,
    status_value: str = Query(alias="status", pattern="^(accepted|rejected|blocked)$"),
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    row = database.get(Connection, connection_id)
    if not row or row.recipient_id != user.id or user.is_bot:
        raise HTTPException(status_code=404, detail="Connection request not found")
    row.status = status_value
    if status_value == "accepted":
        ensure_conversation(database, row.requester_id, row.recipient_id)
    record_audit(
        database,
        f"connection.{status_value}",
        "connection",
        row.id,
        actor_id=user.id,
        actor_type="user",
    )
    database.commit()
    database.refresh(row)
    return row
