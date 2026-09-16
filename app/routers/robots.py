from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import BotProfile, Companion, RobotMemory, User
from ..presenters import companion_response, public_bot_response
from ..schemas import (
    CompanionCreate,
    CompanionResponse,
    CompanionUpdate,
    MemoryCreate,
    MemoryResponse,
    PublicBotResponse,
)
from ..security import current_user
from ..services.agents import enqueue_action
from ..services.audit import record_audit

router = APIRouter(prefix="/api/robots", tags=["robots and companions"])


@router.get("", response_model=list[PublicBotResponse])
def robots(_: User = Depends(current_user), database: Session = Depends(get_db)):
    rows = list(
        database.scalars(
            select(User)
            .join(BotProfile, BotProfile.bot_id == User.id)
            .where(
                User.is_bot.is_(True),
                User.bot_enabled.is_(True),
                User.account_status == "active",
                BotProfile.status == "active",
            )
            .order_by(User.name)
        )
    )
    return [public_bot_response(database, row) for row in rows]


@router.get("/{bot_id}", response_model=PublicBotResponse)
def robot(bot_id: int, _: User = Depends(current_user), database: Session = Depends(get_db)):
    row = database.get(User, bot_id)
    if not row or not row.is_bot or not row.bot_enabled:
        raise HTTPException(status_code=404, detail="Robot not found")
    return public_bot_response(database, row)


@router.get("/companions/mine", response_model=list[CompanionResponse])
def companions(user: User = Depends(current_user), database: Session = Depends(get_db)):
    rows = list(
        database.scalars(
            select(Companion)
            .where(Companion.user_id == user.id)
            .order_by(Companion.created_at.desc())
        )
    )
    return [companion_response(database, row) for row in rows]


@router.post("/companions", response_model=CompanionResponse, status_code=201)
def request_companion(
    payload: CompanionCreate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, payload.bot_id)
    profile = database.get(BotProfile, payload.bot_id)
    if (
        not bot
        or not bot.is_bot
        or not bot.bot_enabled
        or not profile
        or profile.status != "active"
    ):
        raise HTTPException(status_code=404, detail="Robot not found")
    existing = database.scalar(
        select(Companion).where(Companion.user_id == user.id, Companion.bot_id == bot.id)
    )
    if existing:
        raise HTTPException(status_code=409, detail="A companion relationship already exists")
    row = Companion(
        user_id=user.id,
        bot_id=bot.id,
        status="evaluating",
        can_post=payload.can_post,
        can_comment=payload.can_comment,
        can_like=payload.can_like,
        memory_enabled=payload.memory_enabled,
    )
    database.add(row)
    database.flush()
    enqueue_action(
        database,
        bot.id,
        "evaluate_companion",
        target_id=row.id,
        trigger="companion_request",
        idempotency_key=f"companion-eval:{row.id}",
    )
    record_audit(database, "companion.requested", "companion", row.id, actor_id=user.id)
    database.commit()
    database.refresh(row)
    return companion_response(database, row)


@router.patch("/companions/{companion_id}", response_model=CompanionResponse)
def update_companion(
    companion_id: int,
    payload: CompanionUpdate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    row = database.get(Companion, companion_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Companion relationship not found")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(row, key, value)
    if payload.status in {"ended", "blocked"}:
        row.ended_at = datetime.now(timezone.utc)
    if payload.memory_enabled is False:
        for memory in database.scalars(
            select(RobotMemory).where(
                RobotMemory.bot_id == row.bot_id,
                RobotMemory.user_id == row.user_id,
                RobotMemory.active.is_(True),
            )
        ):
            memory.active = False
            memory.embedding = None
            memory.embedding_model = None
            memory.embedding_status = "disabled"
            memory.embedded_at = None
    record_audit(
        database,
        "companion.updated",
        "companion",
        row.id,
        actor_id=user.id,
        metadata={"changes": list(changes)},
    )
    database.commit()
    database.refresh(row)
    return companion_response(database, row)


def _active_companion(database: Session, user_id: int, bot_id: int) -> Companion:
    companion = database.scalar(
        select(Companion).where(Companion.user_id == user_id, Companion.bot_id == bot_id)
    )
    if not companion or companion.status != "active" or not companion.memory_enabled:
        raise HTTPException(status_code=403, detail="Memory is not enabled for this relationship")
    return companion


@router.get("/{bot_id}/memories", response_model=list[MemoryResponse])
def memories(
    bot_id: int,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    _active_companion(database, user.id, bot_id)
    return list(
        database.scalars(
            select(RobotMemory)
            .where(
                RobotMemory.bot_id == bot_id,
                RobotMemory.user_id == user.id,
                RobotMemory.active.is_(True),
            )
            .order_by(RobotMemory.importance.desc(), RobotMemory.created_at.desc())
        )
    )


@router.post("/{bot_id}/memories", response_model=MemoryResponse, status_code=201)
def create_memory(
    bot_id: int,
    payload: MemoryCreate,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    _active_companion(database, user.id, bot_id)
    profile = database.get(BotProfile, bot_id)
    row = RobotMemory(
        bot_id=bot_id,
        organization_id=profile.organization_id if profile else None,
        user_id=user.id,
        kind=payload.kind,
        content=payload.content.strip(),
        importance=payload.importance,
    )
    database.add(row)
    database.flush()
    enqueue_action(
        database,
        bot_id,
        "embed_memory",
        target_id=row.id,
        trigger="memory_created",
        idempotency_key=f"memory-index:{row.id}",
    )
    record_audit(database, "memory.created", "robot_memory", row.id, actor_id=user.id)
    database.commit()
    database.refresh(row)
    return row


@router.delete("/{bot_id}/memories/{memory_id}", status_code=204)
def delete_memory(
    bot_id: int,
    memory_id: int,
    user: User = Depends(current_user),
    database: Session = Depends(get_db),
):
    row = database.get(RobotMemory, memory_id)
    if not row or row.bot_id != bot_id or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Memory not found")
    record_audit(database, "memory.deleted", "robot_memory", row.id, actor_id=user.id)
    database.delete(row)
    database.commit()
    return Response(status_code=204)
