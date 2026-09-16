from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class ActiveHours(BaseModel):
    start: int = Field(default=0, ge=0, le=23)
    end: int = Field(default=24, ge=0, le=24)


def _validate_timezone(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Unknown IANA timezone") from error
    return value


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=120)
    bio: str | None = Field(default=None, max_length=500)
    profile_picture_url: str | None = Field(default=None, max_length=1000)
    sex: str = Field(
        default="prefer_not_to_say", pattern="^(female|male|non_binary|prefer_not_to_say)$"
    )
    password: str = Field(min_length=10, max_length=128)
    preferred_language: str = Field(default="en", min_length=2, max_length=20)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GuestLogin(BaseModel):
    nickname: str = Field(min_length=2, max_length=60)
    sex: str = Field(pattern="^(female|male|non_binary|prefer_not_to_say)$")
    preferred_language: str = Field(min_length=2, max_length=20)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    name: str
    bio: str | None = None
    profile_picture_url: str | None = None
    sex: str | None = None
    preferred_language: str
    is_admin: bool = False
    is_bot: bool = False
    account_status: str = "active"


class PublicUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    bio: str | None = None
    profile_picture_url: str | None = None
    preferred_language: str
    is_bot: bool = False


class PublicBotResponse(PublicUserResponse):
    autonomy_level: str = "supervised"


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class AccountDeleteRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ConnectionCreate(BaseModel):
    recipient_id: int


class ConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requester_id: int
    recipient_id: int
    status: str
    decision_reason: str | None = None
    created_at: datetime
    requester: PublicUserResponse | None = None
    recipient: PublicUserResponse | None = None


class ConversationResponse(BaseModel):
    id: int
    peer: PublicUserResponse
    status: str = "active"


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    source_language: str = Field(default="en", min_length=2, max_length=20)
    client_message_id: str | None = Field(default=None, min_length=8, max_length=100)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender_id: int
    original_text: str
    source_language: str
    translated_text: str
    target_language: str
    is_bot_generated: bool = False
    moderation_status: str = "approved"
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


def message_response_for(message: Any, viewer_id: int) -> MessageResponse:
    if message.sender_id == viewer_id:
        return MessageResponse(
            id=message.id,
            conversation_id=message.conversation_id,
            sender_id=message.sender_id,
            original_text=message.original_text,
            source_language=message.source_language,
            translated_text=message.original_text,
            target_language=message.source_language,
            is_bot_generated=message.is_bot_generated,
            moderation_status=message.moderation_status,
            generation_metadata=message.generation_metadata,
            created_at=message.created_at,
        )
    return MessageResponse.model_validate(message)


class TranslationSettingsResponse(BaseModel):
    provider: str
    api_url: str | None
    model: str | None
    system_prompt: str
    has_api_key: bool
    api_key_ref: str | None = None


class TranslationSettingsUpdate(BaseModel):
    provider: str = Field(pattern="^(local|openai_compatible)$")
    api_url: str | None = None
    api_key: str | None = None
    api_key_ref: str | None = Field(default=None, max_length=500)
    model: str | None = None
    system_prompt: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def one_secret_source(self):
        if self.api_key and self.api_key_ref:
            raise ValueError("Provide an API key or a secret reference, not both")
        return self


class AISettingsResponse(BaseModel):
    provider: str
    api_url: str | None
    model: str | None
    moderation_model: str | None
    embedding_model: str
    embedding_dimensions: int
    embeddings_enabled: bool
    enabled: bool
    has_api_key: bool
    api_key_ref: str | None = None


class AISettingsUpdate(BaseModel):
    provider: str = Field(pattern="^(local|openai_compatible)$")
    api_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    api_key_ref: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=120)
    moderation_model: str | None = Field(default=None, max_length=120)
    embedding_model: str = Field(default="text-embedding-3-small", min_length=1, max_length=120)
    embedding_dimensions: int = Field(default=384, ge=384, le=384)
    embeddings_enabled: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def one_secret_source(self):
        if self.api_key and self.api_key_ref:
            raise ValueError("Provide an API key or a secret reference, not both")
        return self


class PostCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    visibility: str = Field(default="public", pattern="^(public|connections)$")


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    parent_comment_id: int | None = None


class CommentResponse(BaseModel):
    id: int
    post_id: int
    author: PublicUserResponse
    parent_comment_id: int | None
    content: str
    is_bot_generated: bool
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PostResponse(BaseModel):
    id: int
    author: PublicUserResponse
    content: str
    is_bot_generated: bool
    visibility: str = "public"
    created_at: datetime
    likes: int
    comments: int
    liked_by_me: bool
    generation_metadata: dict[str, Any] = Field(default_factory=dict)


class FeedResponse(BaseModel):
    items: list[PostResponse]
    next_cursor: int | None = None


class CompanionCreate(BaseModel):
    bot_id: int
    can_post: bool = False
    can_comment: bool = False
    can_like: bool = False
    memory_enabled: bool = True


class CompanionUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|paused|ended|blocked)$")
    can_post: bool | None = None
    can_comment: bool | None = None
    can_like: bool | None = None
    memory_enabled: bool | None = None


class CompanionResponse(BaseModel):
    id: int
    bot: PublicBotResponse
    status: str
    can_post: bool
    can_comment: bool
    can_like: bool
    memory_enabled: bool
    decision_reason: str | None = None
    created_at: datetime


class BotCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    bio: str | None = Field(default=None, max_length=500)
    profile_picture_url: str | None = Field(default=None, max_length=1000)
    preferred_language: str = Field(default="en", min_length=2, max_length=20)
    sex: str = Field(
        default="prefer_not_to_say", pattern="^(female|male|non_binary|prefer_not_to_say)$"
    )
    bot_prompt: str = Field(min_length=20, max_length=8000)
    bot_ideology: str = Field(default="Be kind, curious, and respectful.", max_length=3000)
    traits: list[str] = Field(default_factory=list, max_length=20)
    goals: list[str] = Field(default_factory=list, max_length=20)
    allowed_topics: list[str] = Field(default_factory=list, max_length=100)
    blocked_topics: list[str] = Field(default_factory=list, max_length=100)
    bot_post_interval_minutes: int = Field(default=1440, ge=15, le=10080)
    bot_autonomous: bool = False
    bot_enabled: bool = True
    autonomy_level: str = Field(default="supervised", pattern="^(manual|supervised|bounded)$")
    max_daily_actions: int = Field(default=24, ge=1, le=1000)
    max_daily_tokens: int = Field(default=50000, ge=1000, le=10_000_000)
    cooldown_seconds: int = Field(default=900, ge=10, le=86400)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    active_hours: ActiveHours = Field(default_factory=ActiveHours)
    max_chain_depth: int = Field(default=1, ge=0, le=5)
    connections_auto_decide: bool = True
    reply_enabled: bool = True
    organization_id: int | None = None

    _timezone = field_validator("timezone")(_validate_timezone)


class BotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    bio: str | None = Field(default=None, max_length=500)
    profile_picture_url: str | None = Field(default=None, max_length=1000)
    bot_enabled: bool | None = None
    bot_autonomous: bool | None = None
    bot_post_interval_minutes: int | None = Field(default=None, ge=15, le=10080)
    status: str | None = Field(default=None, pattern="^(draft|active|paused|quarantined|retired)$")
    autonomy_level: str | None = Field(default=None, pattern="^(manual|supervised|bounded)$")
    max_daily_actions: int | None = Field(default=None, ge=1, le=1000)
    max_daily_tokens: int | None = Field(default=None, ge=1000, le=10_000_000)
    cooldown_seconds: int | None = Field(default=None, ge=10, le=86400)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    active_hours: ActiveHours | None = None
    max_chain_depth: int | None = Field(default=None, ge=0, le=5)
    connections_auto_decide: bool | None = None
    reply_enabled: bool | None = None
    allowed_topics: list[str] | None = Field(default=None, max_length=100)
    blocked_topics: list[str] | None = Field(default=None, max_length=100)

    _timezone = field_validator("timezone")(_validate_timezone)


class BotResponse(UserResponse):
    bot_prompt: str | None = None
    bot_ideology: str | None = None
    bot_post_interval_minutes: int
    bot_autonomous: bool
    bot_enabled: bool
    organization_id: int | None = None
    robot_status: str = "draft"
    autonomy_level: str = "supervised"
    max_daily_actions: int = 24
    max_daily_tokens: int = 50000
    cooldown_seconds: int = 900
    timezone: str = "UTC"
    active_hours: ActiveHours = Field(default_factory=ActiveHours)
    max_chain_depth: int = 1
    connections_auto_decide: bool = True
    reply_enabled: bool = True
    allowed_topics: list[str] = Field(default_factory=list)
    blocked_topics: list[str] = Field(default_factory=list)
    active_persona_version: int | None = None


class PersonaCreate(BaseModel):
    display_name: str = Field(min_length=2, max_length=160)
    system_prompt: str = Field(min_length=20, max_length=8000)
    ideology: str = Field(default="Be kind, curious, and respectful.", max_length=3000)
    traits: list[str] = Field(default_factory=list, max_length=20)
    goals: list[str] = Field(default_factory=list, max_length=20)
    policy: dict[str, Any] = Field(default_factory=dict)
    model: str | None = Field(default=None, max_length=120)
    temperature: float = Field(default=0.7, ge=0, le=1.5)


class PersonaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    version: int
    status: str
    display_name: str
    system_prompt: str
    ideology: str
    traits: list[str]
    goals: list[str]
    policy: dict[str, Any]
    model: str | None
    temperature: float
    created_by_id: int
    created_at: datetime
    published_at: datetime | None


class MemoryCreate(BaseModel):
    kind: str = Field(default="preference", pattern="^(preference|fact|boundary|summary)$")
    content: str = Field(min_length=1, max_length=1000)
    importance: float = Field(default=0.5, ge=0, le=1)


class MemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    user_id: int
    kind: str
    content: str
    importance: float
    active: bool
    embedding_status: str
    created_at: datetime


class BotActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    organization_id: int | None
    action_type: str
    target_id: int | None
    trigger: str
    status: str
    decision: str | None
    reason: str | None
    payload: dict[str, Any]
    result: dict[str, Any]
    retrieval_context: list[dict[str, Any]]
    fleet_generation: int
    priority: int
    scheduled_for: datetime
    attempts: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    created_at: datetime
    completed_at: datetime | None
    locked_by: str | None
    heartbeat_at: datetime | None


class ActionEnqueue(BaseModel):
    action_type: str = Field(pattern="^(generate_post|react_feed)$")


class KnowledgeSourceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    source_type: str = Field(default="text", pattern="^(text|url|document)$")
    uri: str | None = Field(default=None, max_length=1000)
    content: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def require_source(self):
        if not self.uri and not self.content:
            raise ValueError("Either uri or content is required")
        return self


class KnowledgeSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    name: str
    source_type: str
    uri: str | None
    status: str
    trust_level: str
    risk_labels: list[str]
    created_at: datetime


class VectorReindexRequest(BaseModel):
    bot_id: int | None = None
    include_memories: bool = True
    include_knowledge: bool = True

    @model_validator(mode="after")
    def require_scope(self):
        if not self.include_memories and not self.include_knowledge:
            raise ValueError("At least one vector collection must be selected")
        return self


class VectorStatusResponse(BaseModel):
    dimensions: int
    provider: str
    model: str
    enabled: bool
    memories: dict[str, int]
    knowledge_sources: dict[str, int]
    knowledge_chunks: dict[str, int]


class VectorReindexResponse(BaseModel):
    queued_memories: int
    queued_knowledge_sources: int


class ToolDefinitionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120, pattern="^[a-z][a-z0-9_]+$")
    description: str = Field(min_length=5, max_length=2000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    executor_kind: str = Field(default="internal", pattern="^internal$")
    risk_level: str = Field(default="low", pattern="^(low|medium|high|critical)$")
    enabled: bool = False


class ToolDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    input_schema: dict[str, Any]
    executor_kind: str
    risk_level: str
    enabled: bool


class ToolGrantUpdate(BaseModel):
    allowed: bool = False
    requires_approval: bool = True
    constraints_json: dict[str, Any] = Field(default_factory=dict)


class ToolGrantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    organization_id: int | None
    tool_id: int
    allowed: bool
    requires_approval: bool
    constraints_json: dict[str, Any]


class ToolInvocationCreate(BaseModel):
    bot_id: int
    tool_name: str = Field(min_length=2, max_length=120)
    input_json: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    tool_id: int
    status: str
    capability_id: str
    input_json: dict[str, Any]
    result_json: dict[str, Any]
    expires_at: datetime
    completed_at: datetime | None
    capability_token: str | None = None


class ReportCreate(BaseModel):
    entity_type: str = Field(pattern="^(post|comment|message|robot)$")
    entity_id: int
    category: str = Field(min_length=2, max_length=50)
    reason: str = Field(min_length=5, max_length=2000)


class ModerationUpdate(BaseModel):
    status: str = Field(pattern="^(open|investigating|resolved|dismissed)$")
    resolution: str | None = Field(default=None, max_length=2000)


class ModerationCaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    reporter_id: int | None
    assigned_to_id: int | None
    status: str
    severity: str
    category: str
    reason: str
    resolution: str | None
    created_at: datetime
    resolved_at: datetime | None


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int | None
    actor_id: int | None
    actor_type: str
    action: str
    entity_type: str
    entity_id: str | None
    request_id: str | None
    metadata_json: dict[str, Any]
    previous_hash: str | None
    event_hash: str | None
    created_at: datetime


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=2, max_length=100, pattern="^[a-z0-9][a-z0-9-]+$")


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    status: str
    settings: dict[str, Any]
    created_at: datetime


class OrganizationMembershipCreate(BaseModel):
    user_id: int
    role: str = Field(pattern="^(owner|admin|operator|viewer)$")


class OrganizationMembershipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    user_id: int
    role: str
    created_at: datetime


class DashboardResponse(BaseModel):
    humans: int
    robots: int
    active_robots: int
    pending_actions: int
    failed_actions: int
    open_moderation_cases: int
    posts_24h: int
    messages_24h: int
    tokens_24h: int
    estimated_cost_usd_24h: float
    fleet_paused: bool
    oldest_pending_action_seconds: int
    failed_vectors: int


class AuditVerificationResponse(BaseModel):
    valid: bool
    first_invalid_event_id: int | None = None
