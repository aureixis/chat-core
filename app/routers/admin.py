from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..housekeeping import clear_expired_guests
from ..models import (
    AISettings,
    AuditEvent,
    BotAction,
    BotProfile,
    KnowledgeChunk,
    Message,
    ModerationCase,
    Organization,
    OrganizationMembership,
    PersonaVersion,
    PlatformState,
    Post,
    RobotKnowledgeSource,
    RobotMemory,
    RobotToolGrant,
    ToolDefinition,
    ToolInvocation,
    TranslationSettings,
    UsageLedger,
    User,
)
from ..presenters import bot_response
from ..schemas import (
    ActionEnqueue,
    AISettingsResponse,
    AISettingsUpdate,
    AuditEventResponse,
    AuditVerificationResponse,
    BotActionResponse,
    BotCreate,
    BotResponse,
    BotUpdate,
    DashboardResponse,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
    ModerationCaseResponse,
    ModerationUpdate,
    OrganizationCreate,
    OrganizationMembershipCreate,
    OrganizationMembershipResponse,
    OrganizationResponse,
    PersonaCreate,
    PersonaResponse,
    ToolDefinitionCreate,
    ToolDefinitionResponse,
    ToolGrantResponse,
    ToolGrantUpdate,
    ToolInvocationCreate,
    ToolInvocationResponse,
    TranslationSettingsResponse,
    TranslationSettingsUpdate,
    UserResponse,
    VectorReindexRequest,
    VectorReindexResponse,
    VectorStatusResponse,
)
from ..security import admin_user, hash_password
from ..services.agents import enqueue_action, schedule_pending_vectors
from ..services.audit import record_audit, verify_audit_chain
from ..services.embeddings import content_hash
from ..services.policy import evaluate_knowledge_content
from ..services.secrets import SecretResolver, secret_is_configured
from ..services.tools import (
    ToolAuthorizationError,
    approve_tool_invocation,
    issue_capability_token,
    request_tool_invocation,
)
from ..translation import DEFAULT_SYSTEM_PROMPT
from ..vector_config import VECTOR_DIMENSIONS

router = APIRouter(prefix="/api/admin", tags=["administration"])


@router.get("/users", response_model=list[UserResponse])
def users(admin: User = Depends(admin_user), database: Session = Depends(get_db)):
    clear_expired_guests(database)
    return list(database.scalars(select(User).where(User.id != admin.id).order_by(User.name)))


@router.get("/dashboard", response_model=DashboardResponse)
def dashboard(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    scalar = database.scalar
    state = database.get(PlatformState, 1)
    oldest_pending = scalar(
        select(func.min(BotAction.created_at)).where(BotAction.status == "pending")
    )
    if oldest_pending and oldest_pending.tzinfo is None:
        oldest_pending = oldest_pending.replace(tzinfo=timezone.utc)
    return DashboardResponse(
        humans=scalar(select(func.count(User.id)).where(User.is_bot.is_(False))) or 0,
        robots=scalar(select(func.count(User.id)).where(User.is_bot.is_(True))) or 0,
        active_robots=scalar(
            select(func.count(BotProfile.bot_id)).where(BotProfile.status == "active")
        )
        or 0,
        pending_actions=scalar(
            select(func.count(BotAction.id)).where(BotAction.status.in_(["pending", "running"]))
        )
        or 0,
        failed_actions=scalar(select(func.count(BotAction.id)).where(BotAction.status == "dead"))
        or 0,
        open_moderation_cases=scalar(
            select(func.count(ModerationCase.id)).where(
                ModerationCase.status.in_(["open", "investigating"])
            )
        )
        or 0,
        posts_24h=scalar(select(func.count(Post.id)).where(Post.created_at >= since)) or 0,
        messages_24h=scalar(select(func.count(Message.id)).where(Message.created_at >= since)) or 0,
        tokens_24h=scalar(
            select(
                func.coalesce(func.sum(UsageLedger.input_tokens + UsageLedger.output_tokens), 0)
            ).where(UsageLedger.created_at >= since)
        )
        or 0,
        estimated_cost_usd_24h=float(
            scalar(
                select(func.coalesce(func.sum(UsageLedger.estimated_cost_usd), 0)).where(
                    UsageLedger.created_at >= since
                )
            )
            or 0
        ),
        fleet_paused=bool(state and state.fleet_paused),
        oldest_pending_action_seconds=(
            max(0, int((datetime.now(timezone.utc) - oldest_pending).total_seconds()))
            if oldest_pending
            else 0
        ),
        failed_vectors=(
            (
                scalar(
                    select(func.count(RobotMemory.id)).where(
                        RobotMemory.embedding_status == "failed"
                    )
                )
                or 0
            )
            + (
                scalar(
                    select(func.count(RobotKnowledgeSource.id)).where(
                        RobotKnowledgeSource.status == "failed"
                    )
                )
                or 0
            )
        ),
    )


@router.get("/robots", response_model=list[BotResponse])
def robots(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    rows = list(database.scalars(select(User).where(User.is_bot.is_(True)).order_by(User.name)))
    return [bot_response(database, row) for row in rows]


@router.post("/robots", response_model=BotResponse, status_code=201)
def create_robot(
    payload: BotCreate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    if payload.organization_id and not database.get(Organization, payload.organization_id):
        raise HTTPException(status_code=404, detail="Organization not found")
    bot = User(
        email=f"bot-{uuid4().hex}@robots.lamya.app",
        name=payload.name.strip(),
        bio=payload.bio.strip() if payload.bio else None,
        profile_picture_url=payload.profile_picture_url,
        sex=payload.sex,
        preferred_language=payload.preferred_language,
        password_hash=hash_password(uuid4().hex + uuid4().hex),
        is_bot=True,
        bot_enabled=payload.bot_enabled,
        bot_prompt=None,
        bot_ideology=None,
        bot_post_interval_minutes=payload.bot_post_interval_minutes,
        bot_autonomous=payload.bot_autonomous,
    )
    database.add(bot)
    database.flush()
    persona = PersonaVersion(
        bot_id=bot.id,
        organization_id=payload.organization_id,
        version=1,
        status="published" if payload.bot_enabled else "draft",
        display_name=bot.name,
        system_prompt=payload.bot_prompt.strip(),
        ideology=payload.bot_ideology.strip(),
        traits=payload.traits,
        goals=payload.goals,
        created_by_id=admin.id,
        published_at=datetime.now(timezone.utc) if payload.bot_enabled else None,
    )
    database.add(persona)
    database.flush()
    profile = BotProfile(
        bot_id=bot.id,
        organization_id=payload.organization_id,
        status="active" if payload.bot_enabled else "draft",
        autonomy_level=payload.autonomy_level,
        active_persona_version_id=persona.id if payload.bot_enabled else None,
        max_daily_actions=payload.max_daily_actions,
        max_daily_tokens=payload.max_daily_tokens,
        cooldown_seconds=payload.cooldown_seconds,
        timezone=payload.timezone,
        active_hours=payload.active_hours.model_dump(),
        max_chain_depth=payload.max_chain_depth,
        connections_auto_decide=payload.connections_auto_decide,
        reply_enabled=payload.reply_enabled,
        allowed_topics=payload.allowed_topics,
        blocked_topics=payload.blocked_topics,
        next_action_at=datetime.now(timezone.utc) if payload.bot_autonomous else None,
    )
    database.add(profile)
    record_audit(
        database,
        "robot.created",
        "robot",
        bot.id,
        actor_id=admin.id,
        actor_type="admin",
        metadata={"persona_version": 1},
    )
    database.commit()
    database.refresh(bot)
    return bot_response(database, bot)


@router.patch("/robots/{bot_id}", response_model=BotResponse)
def update_robot(
    bot_id: int,
    payload: BotUpdate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, bot_id)
    profile = database.get(BotProfile, bot_id)
    if not bot or not bot.is_bot or not profile:
        raise HTTPException(status_code=404, detail="Robot not found")
    changes = payload.model_dump(exclude_unset=True)
    user_fields = {
        "name",
        "bio",
        "profile_picture_url",
        "bot_enabled",
        "bot_autonomous",
        "bot_post_interval_minutes",
    }
    for key, value in changes.items():
        if key in user_fields:
            setattr(bot, key, value)
        else:
            setattr(profile, key, value)
    if profile.status in {"paused", "quarantined", "retired"} or not bot.bot_enabled:
        database.execute(
            update(BotAction)
            .where(
                BotAction.bot_id == bot.id,
                BotAction.status == "pending",
                BotAction.action_type.notin_(["embed_memory", "embed_knowledge"]),
            )
            .values(status="cancelled", reason="Robot was paused or disabled")
        )
    elif bot.bot_autonomous and profile.next_action_at is None:
        profile.next_action_at = datetime.now(timezone.utc)
    record_audit(
        database,
        "robot.updated",
        "robot",
        bot.id,
        actor_id=admin.id,
        actor_type="admin",
        metadata={"changes": list(changes)},
    )
    database.commit()
    database.refresh(bot)
    return bot_response(database, bot)


@router.post("/robots/pause-all")
def pause_all_robots(admin: User = Depends(admin_user), database: Session = Depends(get_db)):
    state = database.get(PlatformState, 1) or PlatformState(id=1)
    state.fleet_generation = (state.fleet_generation or 0) + 1
    state.fleet_paused = True
    state.paused_at = datetime.now(timezone.utc)
    database.add(state)
    count = database.execute(
        update(BotProfile).where(BotProfile.status == "active").values(status="paused")
    ).rowcount
    database.execute(
        update(BotAction)
        .where(
            BotAction.status == "pending",
            BotAction.action_type.notin_(["embed_memory", "embed_knowledge"]),
        )
        .values(status="cancelled", reason="Global emergency pause")
    )
    record_audit(
        database,
        "robot.fleet.paused",
        "robot_fleet",
        actor_id=admin.id,
        actor_type="admin",
        metadata={"robots": count},
    )
    database.commit()
    return {"paused": count}


@router.post("/robots/resume-all")
def resume_all_robots(admin: User = Depends(admin_user), database: Session = Depends(get_db)):
    state = database.get(PlatformState, 1) or PlatformState(id=1)
    state.fleet_generation = (state.fleet_generation or 0) + 1
    state.fleet_paused = False
    state.resumed_at = datetime.now(timezone.utc)
    database.add(state)
    count = database.execute(
        update(BotProfile).where(BotProfile.status == "paused").values(status="active")
    ).rowcount
    record_audit(
        database,
        "robot.fleet.resumed",
        "robot_fleet",
        actor_id=admin.id,
        actor_type="admin",
        metadata={"robots": count, "fleet_generation": state.fleet_generation},
    )
    database.commit()
    return {"resumed": count, "fleet_generation": state.fleet_generation}


@router.get("/robots/{bot_id}/personas", response_model=list[PersonaResponse])
def personas(bot_id: int, _: User = Depends(admin_user), database: Session = Depends(get_db)):
    return list(
        database.scalars(
            select(PersonaVersion)
            .where(PersonaVersion.bot_id == bot_id)
            .order_by(PersonaVersion.version.desc())
        )
    )


@router.post("/robots/{bot_id}/personas", response_model=PersonaResponse, status_code=201)
def create_persona(
    bot_id: int,
    payload: PersonaCreate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, bot_id)
    if not bot or not bot.is_bot:
        raise HTTPException(status_code=404, detail="Robot not found")
    profile = database.get(BotProfile, bot_id)
    latest = (
        database.scalar(
            select(func.max(PersonaVersion.version)).where(PersonaVersion.bot_id == bot_id)
        )
        or 0
    )
    row = PersonaVersion(
        bot_id=bot_id,
        organization_id=profile.organization_id if profile else None,
        version=latest + 1,
        created_by_id=admin.id,
        **payload.model_dump(),
    )
    database.add(row)
    database.flush()
    record_audit(database, "persona.created", "persona_version", row.id, actor_id=admin.id)
    database.commit()
    database.refresh(row)
    return row


@router.post("/robots/{bot_id}/personas/{persona_id}/publish", response_model=PersonaResponse)
def publish_persona(
    bot_id: int,
    persona_id: int,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    profile = database.get(BotProfile, bot_id)
    persona = database.get(PersonaVersion, persona_id)
    if not profile or not persona or persona.bot_id != bot_id:
        raise HTTPException(status_code=404, detail="Persona version not found")
    for row in database.scalars(
        select(PersonaVersion).where(
            PersonaVersion.bot_id == bot_id, PersonaVersion.status == "published"
        )
    ):
        row.status = "archived"
    persona.status = "published"
    persona.published_at = datetime.now(timezone.utc)
    profile.active_persona_version_id = persona.id
    bot = database.get(User, bot_id)
    # System instructions live in the tenant-scoped persona table, not the
    # platform-wide identity row used by public social queries.
    bot.bot_prompt = None
    bot.bot_ideology = None
    record_audit(database, "persona.published", "persona_version", persona.id, actor_id=admin.id)
    database.commit()
    database.refresh(persona)
    return persona


@router.get("/actions", response_model=list[BotActionResponse])
def actions(
    status_value: str | None = Query(default=None, alias="status"),
    bot_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    query = select(BotAction)
    if status_value:
        query = query.where(BotAction.status == status_value)
    if bot_id:
        query = query.where(BotAction.bot_id == bot_id)
    return list(database.scalars(query.order_by(BotAction.id.desc()).limit(limit)))


@router.post("/robots/{bot_id}/actions", response_model=BotActionResponse, status_code=202)
def queue_action(
    bot_id: int,
    payload: ActionEnqueue,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, bot_id)
    if not bot or not bot.is_bot:
        raise HTTPException(status_code=404, detail="Robot not found")
    row = enqueue_action(
        database,
        bot_id,
        payload.action_type,
        trigger="admin",
        idempotency_key=f"admin:{admin.id}:{bot_id}:{payload.action_type}:{uuid4().hex}",
    )
    record_audit(database, "robot.action.queued", "bot_action", row.id, actor_id=admin.id)
    database.commit()
    database.refresh(row)
    return row


@router.post("/actions/{action_id}/retry", response_model=BotActionResponse)
def retry_action(
    action_id: int, admin: User = Depends(admin_user), database: Session = Depends(get_db)
):
    row = database.get(BotAction, action_id)
    if not row or row.status not in {"dead", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only dead or cancelled actions can be retried")
    row.status = "pending"
    row.attempts = 0
    row.reason = None
    row.scheduled_for = datetime.now(timezone.utc)
    state = database.get(PlatformState, 1)
    row.fleet_generation = state.fleet_generation if state else 1
    record_audit(database, "robot.action.retried", "bot_action", row.id, actor_id=admin.id)
    database.commit()
    database.refresh(row)
    return row


@router.get("/ai", response_model=AISettingsResponse)
def ai_settings(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    row = database.get(AISettings, 1)
    settings = get_settings()
    return AISettingsResponse(
        provider=row.provider if row else settings.ai_provider,
        api_url=(row.api_url if row else None) or settings.ai_api_url,
        model=(row.model if row else None) or settings.ai_model,
        moderation_model=row.moderation_model if row else None,
        embedding_model=(row.embedding_model if row else None) or settings.embedding_model,
        embedding_dimensions=(row.embedding_dimensions if row else settings.embedding_dimensions),
        embeddings_enabled=(row.embeddings_enabled if row else settings.embeddings_enabled),
        enabled=row.enabled if row else True,
        has_api_key=secret_is_configured(
            row.api_key if row else None, row.api_key_ref if row else None
        )
        or bool(settings.ai_api_key),
        api_key_ref=row.api_key_ref if row else None,
    )


@router.put("/ai", response_model=AISettingsResponse)
def update_ai_settings(
    payload: AISettingsUpdate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    row = database.get(AISettings, 1) or AISettings(id=1)
    defaults = get_settings()
    previous_api_url = (row.api_url or defaults.ai_api_url or "").rstrip("/")
    requested_api_url = (payload.api_url or defaults.ai_api_url or "").rstrip("/")
    url_changed = requested_api_url != previous_api_url
    supplied_credential = bool(payload.api_key or payload.api_key_ref)
    existing_credential = secret_is_configured(row.api_key, row.api_key_ref) or bool(
        defaults.ai_api_key
    )
    remote_enabled = payload.provider == "openai_compatible" and (
        payload.enabled or payload.embeddings_enabled
    )
    if (
        defaults.is_production
        and requested_api_url
        and not requested_api_url.startswith("https://")
    ):
        raise HTTPException(status_code=400, detail="AI API URL must use HTTPS in production")
    if remote_enabled and not requested_api_url:
        raise HTTPException(status_code=400, detail="AI API URL is required")
    if remote_enabled and not supplied_credential and not existing_credential:
        raise HTTPException(status_code=400, detail="AI provider credential is required")
    if remote_enabled and url_changed and not supplied_credential:
        raise HTTPException(
            status_code=400,
            detail="Provide a new credential when changing the AI provider URL",
        )
    previous_vector_config = (
        row.provider or defaults.ai_provider,
        row.api_url or defaults.ai_api_url,
        row.embedding_model or defaults.embedding_model,
        row.embedding_dimensions or defaults.embedding_dimensions,
        row.embeddings_enabled
        if row.embeddings_enabled is not None
        else defaults.embeddings_enabled,
    )
    row.provider = payload.provider
    row.api_url = payload.api_url
    row.model = payload.model
    row.moderation_model = payload.moderation_model
    row.embedding_model = payload.embedding_model
    row.embedding_dimensions = payload.embedding_dimensions
    row.embeddings_enabled = payload.embeddings_enabled
    row.enabled = payload.enabled
    if payload.api_key is not None:
        try:
            row.api_key = SecretResolver().protect(payload.api_key)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        row.api_key_ref = None
    if "api_key_ref" in payload.model_fields_set:
        row.api_key_ref = payload.api_key_ref
        if payload.api_key_ref:
            try:
                SecretResolver().resolve(None, payload.api_key_ref)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            row.api_key = None
    database.add(row)
    current_vector_config = (
        row.provider,
        row.api_url or defaults.ai_api_url,
        row.embedding_model or defaults.embedding_model,
        row.embedding_dimensions,
        row.embeddings_enabled,
    )
    if current_vector_config != previous_vector_config:
        _queue_vector_reindex(database, None, True, True)
    record_audit(database, "ai.settings.updated", "ai_settings", 1, actor_id=admin.id)
    database.commit()
    return AISettingsResponse(
        provider=row.provider,
        api_url=row.api_url,
        model=row.model,
        moderation_model=row.moderation_model,
        embedding_model=row.embedding_model or defaults.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        embeddings_enabled=row.embeddings_enabled,
        enabled=row.enabled,
        has_api_key=secret_is_configured(row.api_key, row.api_key_ref) or bool(defaults.ai_api_key),
        api_key_ref=row.api_key_ref,
    )


@router.get("/translation", response_model=TranslationSettingsResponse)
def translation_settings(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    row = database.get(TranslationSettings, 1)
    settings = get_settings()
    return TranslationSettingsResponse(
        provider=row.provider if row else settings.translation_provider,
        api_url=(row.api_url if row else None) or settings.translation_api_url,
        model=(row.model if row else None) or settings.translation_model,
        system_prompt=(row.system_prompt if row else None) or DEFAULT_SYSTEM_PROMPT,
        has_api_key=secret_is_configured(
            row.api_key if row else None, row.api_key_ref if row else None
        )
        or bool(settings.translation_api_key),
        api_key_ref=row.api_key_ref if row else None,
    )


@router.put("/translation", response_model=TranslationSettingsResponse)
def update_translation(
    payload: TranslationSettingsUpdate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    row = database.get(TranslationSettings, 1) or TranslationSettings(id=1)
    defaults = get_settings()
    previous_api_url = (row.api_url or defaults.translation_api_url or "").rstrip("/")
    requested_api_url = (payload.api_url or defaults.translation_api_url or "").rstrip("/")
    url_changed = requested_api_url != previous_api_url
    supplied_credential = bool(payload.api_key or payload.api_key_ref)
    existing_credential = secret_is_configured(row.api_key, row.api_key_ref) or bool(
        defaults.translation_api_key
    )
    if (
        defaults.is_production
        and requested_api_url
        and not requested_api_url.startswith("https://")
    ):
        raise HTTPException(
            status_code=400, detail="Translation API URL must use HTTPS in production"
        )
    if payload.provider == "openai_compatible" and (not requested_api_url or not payload.model):
        raise HTTPException(status_code=400, detail="Translation API URL and model are required")
    if payload.provider == "openai_compatible" and not (supplied_credential or existing_credential):
        raise HTTPException(status_code=400, detail="Translation credential is required")
    if payload.provider == "openai_compatible" and url_changed and not supplied_credential:
        raise HTTPException(
            status_code=400,
            detail="Provide a new credential when changing the translation provider URL",
        )
    row.provider = payload.provider
    row.api_url = payload.api_url
    row.model = payload.model
    row.system_prompt = payload.system_prompt.strip()
    if payload.api_key is not None:
        try:
            row.api_key = SecretResolver().protect(payload.api_key)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        row.api_key_ref = None
    if "api_key_ref" in payload.model_fields_set:
        row.api_key_ref = payload.api_key_ref
        if payload.api_key_ref:
            try:
                SecretResolver().resolve(None, payload.api_key_ref)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            row.api_key = None
    database.add(row)
    record_audit(
        database, "translation.settings.updated", "translation_settings", 1, actor_id=admin.id
    )
    database.commit()
    return TranslationSettingsResponse(
        provider=row.provider,
        api_url=row.api_url,
        model=row.model,
        system_prompt=row.system_prompt,
        has_api_key=secret_is_configured(row.api_key, row.api_key_ref)
        or bool(get_settings().translation_api_key),
        api_key_ref=row.api_key_ref,
    )


@router.post("/organizations", response_model=OrganizationResponse, status_code=201)
def create_organization(
    payload: OrganizationCreate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    if database.scalar(select(Organization).where(Organization.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="Organization slug already exists")
    row = Organization(name=payload.name.strip(), slug=payload.slug)
    database.add(row)
    database.flush()
    database.add(OrganizationMembership(organization_id=row.id, user_id=admin.id, role="owner"))
    record_audit(database, "organization.created", "organization", row.id, actor_id=admin.id)
    database.commit()
    database.refresh(row)
    return row


@router.get("/organizations", response_model=list[OrganizationResponse])
def organizations(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    return list(database.scalars(select(Organization).order_by(Organization.name)))


@router.get(
    "/organizations/{organization_id}/members",
    response_model=list[OrganizationMembershipResponse],
)
def organization_members(
    organization_id: int,
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    if not database.get(Organization, organization_id):
        raise HTTPException(status_code=404, detail="Organization not found")
    return list(
        database.scalars(
            select(OrganizationMembership)
            .where(OrganizationMembership.organization_id == organization_id)
            .order_by(OrganizationMembership.id)
        )
    )


@router.post(
    "/organizations/{organization_id}/members",
    response_model=OrganizationMembershipResponse,
    status_code=201,
)
def upsert_organization_member(
    organization_id: int,
    payload: OrganizationMembershipCreate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    organization = database.get(Organization, organization_id)
    member = database.get(User, payload.user_id)
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not member or member.is_bot:
        raise HTTPException(status_code=404, detail="Human user not found")
    row = database.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == payload.user_id,
        )
    ) or OrganizationMembership(
        organization_id=organization_id,
        user_id=payload.user_id,
    )
    row.role = payload.role
    database.add(row)
    database.flush()
    record_audit(
        database,
        "organization.membership.updated",
        "organization_membership",
        row.id,
        actor_id=admin.id,
        organization_id=organization_id,
        metadata={"user_id": payload.user_id, "role": payload.role},
    )
    database.commit()
    database.refresh(row)
    return row


@router.delete("/organizations/{organization_id}/members/{user_id}", status_code=204)
def remove_organization_member(
    organization_id: int,
    user_id: int,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    row = database.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="Organization membership not found")
    if row.role == "owner":
        owner_count = database.scalar(
            select(func.count(OrganizationMembership.id)).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.role == "owner",
            )
        )
        if (owner_count or 0) <= 1:
            raise HTTPException(status_code=409, detail="Organization must retain an owner")
    record_audit(
        database,
        "organization.membership.removed",
        "organization_membership",
        row.id,
        actor_id=admin.id,
        organization_id=organization_id,
        metadata={"user_id": user_id, "role": row.role},
    )
    database.delete(row)
    database.commit()
    return Response(status_code=204)


@router.post("/robots/{bot_id}/knowledge", response_model=KnowledgeSourceResponse, status_code=201)
def create_knowledge_source(
    bot_id: int,
    payload: KnowledgeSourceCreate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, bot_id)
    if not bot or not bot.is_bot:
        raise HTTPException(status_code=404, detail="Robot not found")
    profile = database.get(BotProfile, bot_id)
    knowledge_policy = evaluate_knowledge_content(payload.content or payload.uri or "")
    row = RobotKnowledgeSource(
        bot_id=bot_id,
        organization_id=profile.organization_id if profile else None,
        status="pending" if knowledge_policy.allowed else "quarantined",
        risk_labels=knowledge_policy.reasons,
        **payload.model_dump(),
    )
    database.add(row)
    database.flush()
    if knowledge_policy.allowed:
        enqueue_action(
            database,
            bot_id,
            "embed_knowledge",
            target_id=row.id,
            trigger="knowledge_created",
            idempotency_key=f"knowledge-index:{row.id}",
        )
    else:
        database.add(
            ModerationCase(
                organization_id=row.organization_id,
                entity_type="knowledge_source",
                entity_id=row.id,
                reporter_id=admin.id,
                status="open",
                severity="high",
                category="knowledge_prompt_injection",
                reason=", ".join(knowledge_policy.reasons),
                evidence={"name": row.name, "content_hash": content_hash(payload.content or "")},
            )
        )
    record_audit(database, "knowledge.created", "knowledge_source", row.id, actor_id=admin.id)
    database.commit()
    database.refresh(row)
    return row


@router.get("/robots/{bot_id}/knowledge", response_model=list[KnowledgeSourceResponse])
def knowledge_sources(
    bot_id: int,
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, bot_id)
    if not bot or not bot.is_bot:
        raise HTTPException(status_code=404, detail="Robot not found")
    return list(
        database.scalars(
            select(RobotKnowledgeSource)
            .where(RobotKnowledgeSource.bot_id == bot_id)
            .order_by(RobotKnowledgeSource.created_at.desc())
            .limit(500)
        )
    )


@router.delete("/robots/{bot_id}/knowledge/{source_id}", status_code=204)
def delete_knowledge_source(
    bot_id: int,
    source_id: int,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    row = database.get(RobotKnowledgeSource, source_id)
    if not row or row.bot_id != bot_id:
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    record_audit(database, "knowledge.deleted", "knowledge_source", row.id, actor_id=admin.id)
    database.delete(row)
    database.commit()
    return Response(status_code=204)


def _status_counts(database: Session, column, *, bot_id: int | None = None) -> dict[str, int]:
    model = column.class_
    query = select(column, func.count()).group_by(column)
    if bot_id is not None:
        query = query.where(model.bot_id == bot_id)
    return {str(status): int(count) for status, count in database.execute(query).all()}


@router.get("/vectors/status", response_model=VectorStatusResponse)
def vector_status(
    bot_id: int | None = None,
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    if bot_id is not None:
        bot = database.get(User, bot_id)
        if not bot or not bot.is_bot:
            raise HTTPException(status_code=404, detail="Robot not found")
    row = database.get(AISettings, 1)
    settings = get_settings()
    return VectorStatusResponse(
        dimensions=VECTOR_DIMENSIONS,
        provider=row.provider if row else settings.ai_provider,
        model=(row.embedding_model if row else None) or settings.embedding_model,
        enabled=row.embeddings_enabled if row else settings.embeddings_enabled,
        memories=_status_counts(database, RobotMemory.embedding_status, bot_id=bot_id),
        knowledge_sources=_status_counts(database, RobotKnowledgeSource.status, bot_id=bot_id),
        knowledge_chunks=_status_counts(database, KnowledgeChunk.embedding_status, bot_id=bot_id),
    )


def _queue_vector_reindex(
    database: Session,
    bot_id: int | None,
    include_memories: bool,
    include_knowledge: bool,
) -> VectorReindexResponse:
    queued_memories = 0
    queued_sources = 0
    if include_memories:
        query = select(RobotMemory).where(RobotMemory.active.is_(True))
        if bot_id is not None:
            query = query.where(RobotMemory.bot_id == bot_id)
        for memory in database.scalars(query):
            memory.embedding_status = "pending"
            queued_memories += 1
    if include_knowledge:
        query = select(RobotKnowledgeSource).where(RobotKnowledgeSource.status != "quarantined")
        if bot_id is not None:
            query = query.where(RobotKnowledgeSource.bot_id == bot_id)
        for source in database.scalars(query):
            source.status = "pending"
            queued_sources += 1
    # Vector rows are the durable staging queue. Materialize only a bounded
    # action batch now; workers continue scanning the remainder.
    schedule_pending_vectors(database, limit=100)
    return VectorReindexResponse(
        queued_memories=queued_memories,
        queued_knowledge_sources=queued_sources,
    )


@router.post("/vectors/reindex", response_model=VectorReindexResponse, status_code=202)
def reindex_vectors(
    payload: VectorReindexRequest,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    if payload.bot_id is not None:
        bot = database.get(User, payload.bot_id)
        if not bot or not bot.is_bot:
            raise HTTPException(status_code=404, detail="Robot not found")
    result = _queue_vector_reindex(
        database,
        payload.bot_id,
        payload.include_memories,
        payload.include_knowledge,
    )
    record_audit(
        database,
        "vectors.reindex.queued",
        "vector_index",
        str(payload.bot_id) if payload.bot_id is not None else "all",
        actor_id=admin.id,
        metadata=result.model_dump(),
    )
    database.commit()
    return result


@router.get("/tools", response_model=list[ToolDefinitionResponse])
def tool_definitions(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    return list(database.scalars(select(ToolDefinition).order_by(ToolDefinition.name)))


@router.get("/tools/invocations", response_model=list[ToolInvocationResponse])
def tool_invocations(
    status_value: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    query = select(ToolInvocation)
    if status_value:
        query = query.where(ToolInvocation.status == status_value)
    return list(database.scalars(query.order_by(ToolInvocation.id.desc()).limit(limit)))


@router.get("/robots/{bot_id}/tools", response_model=list[ToolGrantResponse])
def robot_tool_grants(
    bot_id: int,
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, bot_id)
    if not bot or not bot.is_bot:
        raise HTTPException(status_code=404, detail="Robot not found")
    return list(
        database.scalars(
            select(RobotToolGrant)
            .where(RobotToolGrant.bot_id == bot_id)
            .order_by(RobotToolGrant.tool_id)
        )
    )


@router.post("/tools", response_model=ToolDefinitionResponse, status_code=201)
def create_tool_definition(
    payload: ToolDefinitionCreate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    if database.scalar(select(ToolDefinition.id).where(ToolDefinition.name == payload.name)):
        raise HTTPException(status_code=409, detail="Tool name already exists")
    row = ToolDefinition(**payload.model_dump())
    database.add(row)
    database.flush()
    record_audit(
        database,
        "tool.definition.created",
        "tool_definition",
        row.id,
        actor_id=admin.id,
        metadata={"name": row.name, "risk_level": row.risk_level, "enabled": row.enabled},
    )
    database.commit()
    database.refresh(row)
    return row


@router.put("/robots/{bot_id}/tools/{tool_id}", response_model=ToolGrantResponse)
def update_tool_grant(
    bot_id: int,
    tool_id: int,
    payload: ToolGrantUpdate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    bot = database.get(User, bot_id)
    tool = database.get(ToolDefinition, tool_id)
    profile = database.get(BotProfile, bot_id)
    if not bot or not bot.is_bot or not profile:
        raise HTTPException(status_code=404, detail="Robot not found")
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    row = database.scalar(
        select(RobotToolGrant).where(
            RobotToolGrant.bot_id == bot_id, RobotToolGrant.tool_id == tool_id
        )
    ) or RobotToolGrant(
        bot_id=bot_id,
        organization_id=profile.organization_id,
        tool_id=tool_id,
    )
    row.allowed = payload.allowed
    row.requires_approval = payload.requires_approval
    row.constraints_json = payload.constraints_json
    if tool.risk_level in {"high", "critical"}:
        row.requires_approval = True
    database.add(row)
    database.flush()
    if not row.allowed:
        database.execute(
            update(ToolInvocation)
            .where(
                ToolInvocation.bot_id == bot_id,
                ToolInvocation.tool_id == tool_id,
                ToolInvocation.status.in_(["requested", "approval_required", "authorized"]),
            )
            .values(status="revoked")
        )
    record_audit(
        database,
        "tool.grant.updated",
        "robot_tool_grant",
        row.id,
        actor_id=admin.id,
        organization_id=profile.organization_id,
        metadata={"bot_id": bot_id, "tool": tool.name, "allowed": row.allowed},
    )
    database.commit()
    database.refresh(row)
    return row


@router.post("/tools/invocations", response_model=ToolInvocationResponse, status_code=202)
def create_tool_invocation(
    payload: ToolInvocationCreate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    try:
        row = request_tool_invocation(
            database,
            payload.bot_id,
            payload.tool_name,
            payload.input_json,
            requested_by_id=admin.id,
        )
    except ToolAuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    token = issue_capability_token(row) if row.status == "authorized" else None
    record_audit(
        database,
        "tool.invocation.requested",
        "tool_invocation",
        row.id,
        actor_id=admin.id,
        organization_id=row.organization_id,
        metadata={"bot_id": row.bot_id, "tool_id": row.tool_id, "status": row.status},
    )
    database.commit()
    database.refresh(row)
    return ToolInvocationResponse.model_validate(row).model_copy(update={"capability_token": token})


@router.post("/tools/invocations/{invocation_id}/approve", response_model=ToolInvocationResponse)
def approve_invocation(
    invocation_id: int,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    row = database.get(ToolInvocation, invocation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Tool invocation not found")
    try:
        token = approve_tool_invocation(database, row, admin.id)
    except ToolAuthorizationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    record_audit(
        database,
        "tool.invocation.approved",
        "tool_invocation",
        row.id,
        actor_id=admin.id,
        organization_id=row.organization_id,
    )
    database.commit()
    database.refresh(row)
    return ToolInvocationResponse.model_validate(row).model_copy(update={"capability_token": token})


@router.get("/moderation", response_model=list[ModerationCaseResponse])
def moderation_cases(
    status_value: str | None = Query(default=None, alias="status"),
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    query = select(ModerationCase)
    if status_value:
        query = query.where(ModerationCase.status == status_value)
    return list(database.scalars(query.order_by(ModerationCase.id.desc()).limit(500)))


@router.patch("/moderation/{case_id}", response_model=ModerationCaseResponse)
def update_moderation_case(
    case_id: int,
    payload: ModerationUpdate,
    admin: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    row = database.get(ModerationCase, case_id)
    if not row:
        raise HTTPException(status_code=404, detail="Moderation case not found")
    row.status = payload.status
    row.resolution = payload.resolution
    row.assigned_to_id = admin.id
    if payload.status in {"resolved", "dismissed"}:
        row.resolved_at = datetime.now(timezone.utc)
    if payload.status == "dismissed" and row.entity_type == "knowledge_source":
        source = database.get(RobotKnowledgeSource, row.entity_id)
        if source and source.status == "quarantined":
            source.status = "pending"
            source.trust_level = "reviewed"
            source.risk_labels = []
            enqueue_action(
                database,
                source.bot_id,
                "embed_knowledge",
                target_id=source.id,
                trigger="moderation_approved",
                idempotency_key=f"knowledge-approved:{source.id}:{uuid4().hex}",
            )
    record_audit(database, "moderation.updated", "moderation_case", row.id, actor_id=admin.id)
    database.commit()
    database.refresh(row)
    return row


@router.get("/audit", response_model=list[AuditEventResponse])
def audit_events(
    action: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(admin_user),
    database: Session = Depends(get_db),
):
    query = select(AuditEvent)
    if action:
        query = query.where(AuditEvent.action == action)
    return list(database.scalars(query.order_by(AuditEvent.id.desc()).limit(limit)))


@router.get("/audit/verify", response_model=AuditVerificationResponse)
def verify_audit_log(_: User = Depends(admin_user), database: Session = Depends(get_db)):
    valid, event_id = verify_audit_chain(database)
    return AuditVerificationResponse(valid=valid, first_invalid_event_id=event_id)
