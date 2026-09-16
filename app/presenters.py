from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import BotProfile, Comment, Companion, PersonaVersion, Post, PostLike, User
from .schemas import (
    BotResponse,
    CompanionResponse,
    PostResponse,
    PublicBotResponse,
    PublicUserResponse,
    UserResponse,
)


def user_response(user: User) -> UserResponse:
    return UserResponse.model_validate(user)


def public_user_response(user: User) -> PublicUserResponse:
    return PublicUserResponse.model_validate(user)


def public_bot_response(database: Session, bot: User) -> PublicBotResponse:
    profile = database.get(BotProfile, bot.id)
    return PublicBotResponse(
        **PublicUserResponse.model_validate(bot).model_dump(),
        autonomy_level=profile.autonomy_level if profile else "supervised",
    )


def bot_response(database: Session, bot: User) -> BotResponse:
    profile = database.get(BotProfile, bot.id)
    active_persona = (
        database.get(PersonaVersion, profile.active_persona_version_id)
        if profile and profile.active_persona_version_id
        else None
    )
    return BotResponse(
        **UserResponse.model_validate(bot).model_dump(),
        bot_prompt=active_persona.system_prompt if active_persona else bot.bot_prompt,
        bot_ideology=active_persona.ideology if active_persona else bot.bot_ideology,
        bot_post_interval_minutes=bot.bot_post_interval_minutes,
        bot_autonomous=bot.bot_autonomous,
        bot_enabled=bot.bot_enabled,
        organization_id=profile.organization_id if profile else None,
        robot_status=profile.status if profile else "draft",
        autonomy_level=profile.autonomy_level if profile else "supervised",
        max_daily_actions=profile.max_daily_actions if profile else 24,
        max_daily_tokens=profile.max_daily_tokens if profile else 50000,
        cooldown_seconds=profile.cooldown_seconds if profile else 900,
        timezone=profile.timezone if profile else "UTC",
        active_hours=profile.active_hours if profile else {"start": 0, "end": 24},
        max_chain_depth=profile.max_chain_depth if profile else 1,
        connections_auto_decide=profile.connections_auto_decide if profile else True,
        reply_enabled=profile.reply_enabled if profile else True,
        allowed_topics=profile.allowed_topics if profile else [],
        blocked_topics=profile.blocked_topics if profile else [],
        active_persona_version=active_persona.version if active_persona else None,
    )


def post_response(database: Session, post: Post, viewer_id: int) -> PostResponse:
    author = database.get(User, post.author_id)
    likes = database.scalar(select(func.count(PostLike.id)).where(PostLike.post_id == post.id)) or 0
    comments = (
        database.scalar(
            select(func.count(Comment.id)).where(
                Comment.post_id == post.id, Comment.status == "published"
            )
        )
        or 0
    )
    liked = database.scalar(
        select(PostLike.id).where(PostLike.post_id == post.id, PostLike.user_id == viewer_id)
    )
    return PostResponse(
        id=post.id,
        author=public_user_response(author),
        content=post.content,
        is_bot_generated=post.is_bot_generated,
        visibility=post.visibility,
        created_at=post.created_at,
        likes=likes,
        comments=comments,
        liked_by_me=bool(liked),
        generation_metadata=post.generation_metadata or {},
    )


def companion_response(database: Session, companion: Companion) -> CompanionResponse:
    return CompanionResponse(
        id=companion.id,
        bot=public_bot_response(database, database.get(User, companion.bot_id)),
        status=companion.status,
        can_post=companion.can_post,
        can_comment=companion.can_comment,
        can_like=companion.can_like,
        memory_enabled=companion.memory_enabled,
        decision_reason=companion.decision_reason,
        created_at=companion.created_at,
    )
