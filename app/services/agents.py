from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    BotAction,
    BotProfile,
    Comment,
    Companion,
    Connection,
    Conversation,
    KnowledgeChunk,
    Message,
    ModerationCase,
    PlatformState,
    Post,
    PostLike,
    RobotKnowledgeSource,
    RobotMemory,
    UsageLedger,
    User,
)
from .ai import AgentModelService, AIResult
from .audit import record_audit
from .embeddings import EmbeddingResult, EmbeddingService, chunk_text, content_hash
from .moderation import ModerationService

logger = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{uuid4().hex[:12]}"
_BOT_LOCK_NAMESPACE = 1279348057
_QUEUE_LOCK_NAMESPACE = 1279348058
_ORG_QUEUE_LOCK_NAMESPACE = 1279348059
_MAINTENANCE_ACTIONS = {"embed_memory", "embed_knowledge"}
_ACTION_PRIORITIES = {
    "reply_message": 90,
    "evaluate_companion": 80,
    "evaluate_connection": 80,
    "embed_memory": 70,
    "embed_knowledge": 65,
    "react_feed": 40,
    "generate_post": 30,
}


class ActionCancelledError(RuntimeError):
    pass


class ActionDeferredError(RuntimeError):
    def __init__(self, reason: str, scheduled_for: datetime):
        super().__init__(reason)
        self.scheduled_for = scheduled_for


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _acquire_bot_execution_lock(database: Session, bot_id: int) -> None:
    if not database.bind or database.bind.dialect.name != "postgresql":
        return
    acquired = database.scalar(select(func.pg_try_advisory_xact_lock(_BOT_LOCK_NAMESPACE, bot_id)))
    if not acquired:
        raise ActionDeferredError(
            "Another action is already executing for this robot",
            utcnow() + timedelta(seconds=5),
        )


def _acquire_queue_locks(database: Session, bot_id: int, organization_id: int | None) -> None:
    if not database.bind or database.bind.dialect.name != "postgresql":
        return
    database.execute(select(func.pg_advisory_xact_lock(_QUEUE_LOCK_NAMESPACE, bot_id)))
    if organization_id is not None:
        database.execute(
            select(func.pg_advisory_xact_lock(_ORG_QUEUE_LOCK_NAMESPACE, organization_id))
        )


def _assert_execution_allowed(database: Session, action: BotAction) -> None:
    state = database.execute(
        select(PlatformState.fleet_generation, PlatformState.fleet_paused).where(
            PlatformState.id == 1
        )
    ).one_or_none()
    if state and state.fleet_paused:
        raise ActionCancelledError("Global fleet pause is active")
    if state and action.fleet_generation != state.fleet_generation:
        raise ActionCancelledError("Action predates the latest fleet control change")
    robot_state = database.execute(
        select(User.bot_enabled, BotProfile.status)
        .join(BotProfile, BotProfile.bot_id == User.id)
        .where(User.id == action.bot_id)
    ).one_or_none()
    if not robot_state or not robot_state.bot_enabled or robot_state.status != "active":
        raise ActionCancelledError("Robot is paused, disabled, or unavailable")


def _enforce_operating_window(action: BotAction, profile: BotProfile) -> None:
    if action.trigger != "schedule":
        return
    now = utcnow()
    if profile.last_action_at:
        last_action = profile.last_action_at
        if last_action.tzinfo is None:
            last_action = last_action.replace(tzinfo=timezone.utc)
        ready_at = last_action + timedelta(seconds=profile.cooldown_seconds)
        if ready_at > now:
            raise ActionDeferredError("Robot cooldown is active", ready_at)

    try:
        zone = ZoneInfo(profile.timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local_now = now.astimezone(zone)
    configured = profile.active_hours or {}
    start = min(max(int(configured.get("start", 0)), 0), 23)
    end = min(max(int(configured.get("end", 24)), 0), 24)
    hour = local_now.hour
    active = start <= hour < end if start < end else hour >= start or hour < end
    if active:
        return
    days = 0
    if start < end and hour >= end:
        days = 1
    next_local = (local_now + timedelta(days=days)).replace(
        hour=start, minute=0, second=0, microsecond=0
    )
    if next_local <= local_now:
        next_local += timedelta(days=1)
    raise ActionDeferredError(
        "Robot is outside configured active hours", next_local.astimezone(timezone.utc)
    )


def ensure_conversation(database: Session, first_id: int, second_id: int) -> Conversation:
    first, second = sorted((first_id, second_id))
    conversation = database.scalar(
        select(Conversation).where(
            Conversation.user_a_id == first, Conversation.user_b_id == second
        )
    )
    if not conversation:
        conversation = Conversation(user_a_id=first, user_b_id=second)
        database.add(conversation)
        database.flush()
    return conversation


def enqueue_action(
    database: Session,
    bot_id: int,
    action_type: str,
    *,
    target_id: int | None = None,
    trigger: str = "system",
    payload: dict | None = None,
    idempotency_key: str | None = None,
    scheduled_for: datetime | None = None,
) -> BotAction:
    key = idempotency_key or f"{action_type}:{bot_id}:{target_id or 'none'}:{uuid4().hex}"
    existing = database.scalar(select(BotAction).where(BotAction.idempotency_key == key))
    if existing:
        return existing
    profile = database.get(BotProfile, bot_id)
    _acquire_queue_locks(database, bot_id, profile.organization_id if profile else None)
    state = database.get(PlatformState, 1)
    if not state:
        state = PlatformState(id=1)
        database.add(state)
        database.flush()
    pending_count = (
        database.scalar(
            select(func.count(BotAction.id)).where(
                BotAction.bot_id == bot_id,
                BotAction.status.in_(["pending", "running"]),
            )
        )
        or 0
    )
    organization_pending_count = 0
    if profile and profile.organization_id is not None:
        organization_pending_count = (
            database.scalar(
                select(func.count(BotAction.id)).where(
                    BotAction.organization_id == profile.organization_id,
                    BotAction.status.in_(["pending", "running"]),
                )
            )
            or 0
        )
    settings = get_settings()
    at_capacity = action_type not in _MAINTENANCE_ACTIONS and (
        pending_count >= settings.max_pending_actions_per_bot
        or organization_pending_count >= settings.max_pending_actions_per_organization
    )
    action = BotAction(
        bot_id=bot_id,
        organization_id=profile.organization_id if profile else None,
        action_type=action_type,
        target_id=target_id,
        trigger=trigger,
        payload=payload or {},
        idempotency_key=key,
        scheduled_for=scheduled_for or utcnow(),
        fleet_generation=state.fleet_generation,
        priority=_ACTION_PRIORITIES.get(action_type, 50),
        status="cancelled" if at_capacity else "pending",
        decision="blocked" if at_capacity else None,
        reason="Robot or organization queue capacity reached" if at_capacity else None,
    )
    try:
        with database.begin_nested():
            database.add(action)
            database.flush()
    except IntegrityError:
        existing = database.scalar(select(BotAction).where(BotAction.idempotency_key == key))
        if not existing:
            raise
        return existing
    return action


def schedule_due_robots(database: Session, limit: int = 100) -> int:
    now = utcnow()
    state = database.get(PlatformState, 1)
    if state and state.fleet_paused:
        return 0
    rows = list(
        database.scalars(
            select(BotProfile)
            .join(User, User.id == BotProfile.bot_id)
            .where(
                User.is_bot.is_(True),
                User.bot_enabled.is_(True),
                User.bot_autonomous.is_(True),
                BotProfile.status == "active",
                or_(BotProfile.next_action_at.is_(None), BotProfile.next_action_at <= now),
            )
            .order_by(BotProfile.next_action_at.asc())
            .limit(limit)
        )
    )
    for profile in rows:
        bot = database.get(User, profile.bot_id)
        slot = int(now.timestamp() // max(bot.bot_post_interval_minutes * 60, 60))
        enqueue_action(
            database,
            bot.id,
            "generate_post",
            trigger="schedule",
            idempotency_key=f"scheduled-post:{bot.id}:{slot}",
        )
        enqueue_action(
            database,
            bot.id,
            "react_feed",
            trigger="schedule",
            idempotency_key=f"scheduled-reaction:{bot.id}:{slot}",
            scheduled_for=now + timedelta(seconds=30),
        )
        profile.next_action_at = now + timedelta(minutes=bot.bot_post_interval_minutes)
    return len(rows)


def schedule_pending_vectors(database: Session, limit: int = 100) -> int:
    scheduled = 0
    active_statuses = ["pending", "running"]
    memories = list(
        database.scalars(
            select(RobotMemory)
            .where(RobotMemory.active.is_(True), RobotMemory.embedding_status == "pending")
            .order_by(RobotMemory.id)
            .limit(limit)
        )
    )
    for memory in memories:
        existing = database.scalar(
            select(BotAction.id).where(
                BotAction.action_type == "embed_memory",
                BotAction.target_id == memory.id,
                BotAction.status.in_(active_statuses),
            )
        )
        if existing is None:
            enqueue_action(
                database,
                memory.bot_id,
                "embed_memory",
                target_id=memory.id,
                trigger="pending_vector_scan",
            )
            scheduled += 1

    remaining = max(limit - scheduled, 0)
    sources = list(
        database.scalars(
            select(RobotKnowledgeSource)
            .where(RobotKnowledgeSource.status == "pending")
            .order_by(RobotKnowledgeSource.id)
            .limit(remaining)
        )
    )
    for source in sources:
        existing = database.scalar(
            select(BotAction.id).where(
                BotAction.action_type == "embed_knowledge",
                BotAction.target_id == source.id,
                BotAction.status.in_(active_statuses),
            )
        )
        if existing is None:
            enqueue_action(
                database,
                source.bot_id,
                "embed_knowledge",
                target_id=source.id,
                trigger="pending_vector_scan",
            )
            scheduled += 1
    return scheduled


def claim_next_action(database: Session) -> BotAction | None:
    now = utcnow()
    query = (
        select(BotAction)
        .where(
            BotAction.status == "pending",
            BotAction.scheduled_for <= now,
            or_(BotAction.locked_until.is_(None), BotAction.locked_until < now),
        )
        .order_by(BotAction.priority.desc(), BotAction.scheduled_for, BotAction.id)
        .limit(1)
    )
    if database.bind and database.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    action = database.scalar(query)
    if not action:
        return None
    action.status = "running"
    action.started_at = now
    action.locked_until = now + timedelta(seconds=get_settings().agent_lease_seconds)
    action.locked_by = WORKER_ID
    action.heartbeat_at = now
    action.attempts += 1
    database.commit()
    database.refresh(action)
    return action


def recover_expired_actions(database: Session) -> int:
    """Return abandoned leases to the queue after a worker crash."""
    now = utcnow()
    result = database.execute(
        update(BotAction)
        .where(
            BotAction.status == "running",
            BotAction.locked_until.is_not(None),
            BotAction.locked_until < now,
        )
        .values(
            status="pending",
            scheduled_for=now,
            locked_until=None,
            locked_by=None,
            heartbeat_at=now,
            reason="Recovered after worker lease expired",
        )
    )
    return int(result.rowcount or 0)


def execute_next_action(database: Session) -> BotAction | None:
    action = claim_next_action(database)
    if not action:
        return None
    try:
        _acquire_bot_execution_lock(database, action.bot_id)
        _execute(database, action)
        action.status = "completed"
        action.completed_at = utcnow()
        action.locked_until = None
        action.locked_by = None
        action.heartbeat_at = utcnow()
        profile = database.get(BotProfile, action.bot_id)
        if profile and action.action_type not in {"embed_memory", "embed_knowledge"}:
            profile.last_action_at = action.completed_at
        record_audit(
            database,
            "robot.action.completed",
            "bot_action",
            action.id,
            actor_id=action.bot_id,
            actor_type="robot",
            metadata={"type": action.action_type, "decision": action.decision},
        )
        database.commit()
    except ActionDeferredError as error:
        database.rollback()
        deferred = database.get(BotAction, action.id)
        deferred.status = "pending"
        deferred.attempts = max(deferred.attempts - 1, 0)
        deferred.reason = str(error)[:2000]
        deferred.scheduled_for = error.scheduled_for
        deferred.locked_until = None
        deferred.locked_by = None
        deferred.heartbeat_at = utcnow()
        database.commit()
        return deferred
    except ActionCancelledError as error:
        database.rollback()
        cancelled = database.get(BotAction, action.id)
        cancelled.status = "cancelled"
        cancelled.decision = "cancelled"
        cancelled.reason = str(error)[:2000]
        cancelled.completed_at = utcnow()
        cancelled.locked_until = None
        cancelled.locked_by = None
        cancelled.heartbeat_at = cancelled.completed_at
        record_audit(
            database,
            "robot.action.cancelled",
            "bot_action",
            cancelled.id,
            actor_id=cancelled.bot_id,
            actor_type="robot",
            metadata={"type": cancelled.action_type, "reason": cancelled.reason},
            organization_id=cancelled.organization_id,
        )
        database.commit()
        return cancelled
    except Exception as error:
        database.rollback()
        failed = database.get(BotAction, action.id)
        failed.reason = str(error)[:2000]
        failed.locked_until = None
        failed.locked_by = None
        failed.heartbeat_at = utcnow()
        failed.status = "dead" if failed.attempts >= failed.max_attempts else "pending"
        failed.scheduled_for = utcnow() + timedelta(seconds=30 * (2 ** max(failed.attempts - 1, 0)))
        if failed.status == "dead" and failed.action_type == "embed_memory":
            memory = database.get(RobotMemory, failed.target_id)
            if memory:
                memory.embedding_status = "failed"
        if failed.status == "dead" and failed.action_type == "embed_knowledge":
            source = database.get(RobotKnowledgeSource, failed.target_id)
            if source:
                source.status = "failed"
        record_audit(
            database,
            "robot.action.failed",
            "bot_action",
            failed.id,
            actor_id=failed.bot_id,
            actor_type="robot",
            metadata={"type": failed.action_type, "error": failed.reason},
        )
        database.commit()
        logger.exception("Robot action %s failed", action.id)
        return failed
    return action


def _execute(database: Session, action: BotAction) -> None:
    bot = database.get(User, action.bot_id)
    profile = database.get(BotProfile, action.bot_id)
    if not bot or not bot.is_bot:
        raise ValueError("Robot is unavailable")
    if action.action_type == "embed_memory":
        _embed_memory(database, action)
        return
    if action.action_type == "embed_knowledge":
        _embed_knowledge(database, action)
        return
    if not profile:
        raise ActionCancelledError("Robot profile is unavailable")
    _assert_execution_allowed(database, action)
    _enforce_operating_window(action, profile)
    if not _within_budget(database, profile):
        action.decision = "blocked"
        action.reason = "Daily action or token budget exhausted"
        action.result = {"policy": "daily_budget"}
        return

    model = AgentModelService(database, action.id)
    if action.action_type == "generate_post":
        result = model.generate_post(bot)
        _assert_execution_allowed(database, action)
        if not _budget_allows_result(database, action, profile, result):
            return
        content = str(result.data.get("content", "")).strip()
        if not _validate_generated(database, action, profile, content, "post"):
            _record_usage(database, action, result)
            return
        post = Post(
            author_id=bot.id,
            content=content[:2000],
            is_bot_generated=True,
            correlation_id=action.idempotency_key,
            generation_metadata=_generation_metadata(action, profile, result),
        )
        database.add(post)
        database.flush()
        action.decision = "post"
        action.result = {"post_id": post.id}
        _record_usage(database, action, result)
        return

    if action.action_type == "react_feed":
        post = database.scalar(
            select(Post)
            .where(
                Post.author_id != bot.id,
                Post.status == "published",
                Post.moderation_status == "approved",
                Post.id.notin_(select(Comment.post_id).where(Comment.author_id == bot.id)),
                Post.id.notin_(select(PostLike.post_id).where(PostLike.user_id == bot.id)),
            )
            .order_by(Post.created_at.desc())
            .limit(1)
        )
        if not post:
            action.decision = "none"
            action.reason = "No eligible post"
            return
        result = model.react_to_post(bot, post.content)
        _assert_execution_allowed(database, action)
        if not _budget_allows_result(database, action, profile, result):
            return
        decision = str(result.data.get("action", "none"))
        if decision == "like":
            exists = database.scalar(
                select(PostLike).where(PostLike.post_id == post.id, PostLike.user_id == bot.id)
            )
            if not exists:
                database.add(PostLike(post_id=post.id, user_id=bot.id))
        elif decision == "comment":
            content = str(result.data.get("content", "")).strip()
            if not _validate_generated(database, action, profile, content, "comment"):
                _record_usage(database, action, result)
                return
            database.add(
                Comment(
                    post_id=post.id,
                    author_id=bot.id,
                    content=content[:1000],
                    is_bot_generated=True,
                    generation_metadata=_generation_metadata(action, profile, result),
                )
            )
        elif decision != "none":
            raise ValueError("AI proposed an unsupported reaction")
        action.target_id = post.id
        action.decision = decision
        action.result = {"post_id": post.id}
        _record_usage(database, action, result)
        return

    if action.action_type == "evaluate_connection":
        connection = database.get(Connection, action.target_id)
        if not connection or connection.recipient_id != bot.id:
            raise ValueError("Connection request is unavailable")
        human = database.get(User, connection.requester_id)
        result = model.decide_relationship(bot, human, "connection")
        _assert_execution_allowed(database, action)
        if not _budget_allows_result(database, action, profile, result):
            return
        decision = str(result.data.get("decision", "decline"))
        connection.status = {"accept": "accepted", "waitlist": "waitlisted"}.get(
            decision, "rejected"
        )
        connection.decision_reason = str(result.data.get("reason", ""))[:2000]
        if connection.status == "accepted":
            ensure_conversation(database, bot.id, human.id)
        action.decision = decision
        action.reason = connection.decision_reason
        action.result = {"connection_id": connection.id, "status": connection.status}
        _record_usage(database, action, result)
        return

    if action.action_type == "evaluate_companion":
        companion = database.get(Companion, action.target_id)
        if not companion or companion.bot_id != bot.id:
            raise ValueError("Companion request is unavailable")
        human = database.get(User, companion.user_id)
        result = model.decide_relationship(bot, human, "companionship")
        _assert_execution_allowed(database, action)
        if not _budget_allows_result(database, action, profile, result):
            return
        decision = str(result.data.get("decision", "decline"))
        companion.status = {"accept": "active", "waitlist": "waitlisted"}.get(decision, "declined")
        companion.decision_reason = str(result.data.get("reason", ""))[:2000]
        if companion.status == "active":
            companion.accepted_at = utcnow()
            ensure_conversation(database, bot.id, human.id)
        action.decision = decision
        action.reason = companion.decision_reason
        action.result = {"companion_id": companion.id, "status": companion.status}
        _record_usage(database, action, result)
        return

    if action.action_type == "reply_message":
        source = database.get(Message, action.target_id)
        if not source:
            raise ValueError("Source message is unavailable")
        existing = database.scalar(
            select(Message).where(Message.in_reply_to_id == source.id, Message.sender_id == bot.id)
        )
        if existing:
            action.decision = "duplicate"
            action.result = {"message_id": existing.id}
            return
        conversation = database.get(Conversation, source.conversation_id)
        if not conversation or bot.id not in (conversation.user_a_id, conversation.user_b_id):
            raise ValueError("Robot is not part of the conversation")
        if source.agent_chain_depth >= profile.max_chain_depth:
            action.decision = "none"
            action.reason = "Maximum robot chain depth reached"
            return
        human_id = (
            conversation.user_b_id if conversation.user_a_id == bot.id else conversation.user_a_id
        )
        human = database.get(User, human_id)
        rows = list(
            database.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc())
                .limit(20)
            )
        )
        rows.reverse()
        transcript = [
            {
                "role": "assistant" if row.sender_id == bot.id else "user",
                "content": row.original_text,
            }
            for row in rows
        ]
        result = model.reply(bot, human, transcript)
        _assert_execution_allowed(database, action)
        if not _budget_allows_result(database, action, profile, result):
            return
        content = str(result.data.get("content", "")).strip()
        if not _validate_generated(database, action, profile, content, "message"):
            _record_usage(database, action, result)
            return
        reply = Message(
            conversation_id=conversation.id,
            sender_id=bot.id,
            original_text=content[:4000],
            source_language=bot.preferred_language,
            translated_text=content[:4000],
            target_language=human.preferred_language,
            in_reply_to_id=source.id,
            is_bot_generated=True,
            agent_chain_depth=source.agent_chain_depth + 1,
            generation_metadata=_generation_metadata(action, profile, result),
        )
        database.add(reply)
        database.flush()
        companion = database.scalar(
            select(Companion).where(
                Companion.bot_id == bot.id,
                Companion.user_id == human.id,
                Companion.status == "active",
            )
        )
        if companion:
            companion.last_interaction_at = utcnow()
        action.decision = "reply"
        action.result = {"message_id": reply.id, "conversation_id": conversation.id}
        _record_usage(database, action, result)
        return

    raise ValueError(f"Unsupported action type: {action.action_type}")


def _within_budget(database: Session, profile: BotProfile) -> bool:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    actions = (
        database.scalar(
            select(func.count(BotAction.id)).where(
                BotAction.bot_id == profile.bot_id,
                BotAction.status == "completed",
                BotAction.completed_at >= start,
                BotAction.action_type.notin_(["embed_memory", "embed_knowledge"]),
            )
        )
        or 0
    )
    tokens = (
        database.scalar(
            select(
                func.coalesce(func.sum(BotAction.input_tokens + BotAction.output_tokens), 0)
            ).where(
                BotAction.bot_id == profile.bot_id,
                BotAction.created_at >= start,
            )
        )
        or 0
    )
    return actions < profile.max_daily_actions and tokens < profile.max_daily_tokens


def _budget_allows_result(
    database: Session,
    action: BotAction,
    profile: BotProfile,
    result: AIResult,
) -> bool:
    """Account for already-spent model tokens before allowing a side effect."""
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    tokens_before_result = (
        database.scalar(
            select(
                func.coalesce(func.sum(BotAction.input_tokens + BotAction.output_tokens), 0)
            ).where(
                BotAction.bot_id == profile.bot_id,
                BotAction.created_at >= start,
            )
        )
        or 0
    )
    projected = int(tokens_before_result) + result.input_tokens + result.output_tokens
    if projected <= profile.max_daily_tokens:
        return True
    _record_usage(database, action, result)
    action.decision = "blocked"
    action.reason = "Daily token budget would be exceeded"
    action.result = {
        "policy": "daily_token_budget",
        "projected_tokens": projected,
        "limit": profile.max_daily_tokens,
    }
    return False


def _validate_generated(
    database: Session, action: BotAction, profile: BotProfile, content: str, entity_type: str
) -> bool:
    decision = ModerationService(database).evaluate(
        content, generated=True, blocked_topics=profile.blocked_topics
    )
    action.policy_snapshot = {"status": decision.status, "reasons": decision.reasons}
    if not decision.allowed:
        database.add(
            ModerationCase(
                organization_id=action.organization_id,
                entity_type="bot_action",
                entity_id=action.id,
                status="open",
                severity="high",
                category="generated_content_policy",
                reason=", ".join(decision.reasons),
                evidence={"proposed_type": entity_type, "content": content[:2000]},
            )
        )
        action.decision = "blocked"
        action.reason = "Generated content was blocked by policy"
        action.result = {"policy": decision.reasons}
        return False
    return True


def _record_usage(database: Session, action: BotAction, result: AIResult) -> None:
    action.input_tokens = (action.input_tokens or 0) + result.input_tokens
    action.output_tokens = (action.output_tokens or 0) + result.output_tokens
    database.add(
        UsageLedger(
            bot_id=action.bot_id,
            organization_id=action.organization_id,
            action_id=action.id,
            provider=result.provider,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            estimated_cost_usd=0,
        )
    )


def _generation_metadata(
    action: BotAction, profile: BotProfile, result: AIResult
) -> dict[str, object]:
    return {
        "action_id": action.id,
        "provider": result.provider,
        "model": result.model,
        "persona_version_id": profile.active_persona_version_id,
        "retrieval": action.retrieval_context or [],
    }


def _record_embedding_usage(database: Session, action: BotAction, result: EmbeddingResult) -> None:
    action.input_tokens = result.input_tokens
    action.output_tokens = 0
    database.add(
        UsageLedger(
            bot_id=action.bot_id,
            organization_id=action.organization_id,
            action_id=action.id,
            provider=result.provider,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=0,
            estimated_cost_usd=0,
        )
    )


def _embed_memory(database: Session, action: BotAction) -> None:
    memory = database.get(RobotMemory, action.target_id)
    if not memory or memory.bot_id != action.bot_id:
        action.decision = "skipped"
        action.reason = "Memory was deleted before indexing"
        return
    service = EmbeddingService(database)
    if not memory.active or not service.enabled:
        memory.embedding = None
        memory.embedding_model = None
        memory.embedding_status = "disabled"
        memory.embedded_at = None
        action.decision = "disabled"
        action.result = {"memory_id": memory.id}
        return
    result = service.embed_texts([memory.content])
    memory.embedding = result.vectors[0]
    memory.embedding_model = result.model
    memory.embedding_status = "ready"
    memory.content_hash = content_hash(memory.content)
    memory.embedded_at = utcnow()
    action.decision = "indexed"
    action.result = {"memory_id": memory.id, "dimensions": len(result.vectors[0])}
    _record_embedding_usage(database, action, result)


def _embed_knowledge(database: Session, action: BotAction) -> None:
    source = database.get(RobotKnowledgeSource, action.target_id)
    if not source or source.bot_id != action.bot_id:
        action.decision = "skipped"
        action.reason = "Knowledge source was deleted before indexing"
        return
    service = EmbeddingService(database)
    database.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id))
    if not service.enabled:
        source.status = "disabled"
        action.decision = "disabled"
        action.result = {"source_id": source.id, "chunks": 0}
        return
    source_text = (source.content or source.uri or "").strip()
    chunks = chunk_text(source_text)
    if not chunks:
        raise ValueError("Knowledge source contains no indexable text")
    result = service.embed_texts(chunks)
    embedded_at = utcnow()
    for index, (content, vector) in enumerate(zip(chunks, result.vectors, strict=True)):
        database.add(
            KnowledgeChunk(
                source_id=source.id,
                bot_id=source.bot_id,
                organization_id=source.organization_id,
                chunk_index=index,
                content=content,
                content_hash=content_hash(content),
                embedding=vector,
                embedding_model=result.model,
                embedding_status="ready",
                embedded_at=embedded_at,
            )
        )
    source.status = "ready"
    source.metadata_json = {
        **(source.metadata_json or {}),
        "content_hash": content_hash(source_text),
        "chunk_count": len(chunks),
        "embedding_model": result.model,
        "embedded_at": embedded_at.isoformat(),
    }
    action.decision = "indexed"
    action.result = {
        "source_id": source.id,
        "chunks": len(chunks),
        "dimensions": len(result.vectors[0]),
    }
    _record_embedding_usage(database, action, result)
