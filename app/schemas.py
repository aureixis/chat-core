from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=120)
    bio: str | None = Field(default=None, max_length=500)
    profile_picture_url: str | None = Field(default=None, max_length=1000)
    sex: str = Field(default="prefer_not_to_say", pattern="^(female|male|non_binary|prefer_not_to_say)$")
    password: str = Field(min_length=8, max_length=128)
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


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class ConnectionCreate(BaseModel):
    recipient_id: int


class ConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requester_id: int
    recipient_id: int
    status: str
    created_at: datetime
    requester: UserResponse | None = None
    recipient: UserResponse | None = None


class ConversationResponse(BaseModel):
    id: int
    peer: UserResponse


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender_id: int
    original_text: str
    source_language: str
    translated_text: str
    target_language: str
    created_at: datetime


def message_response_for(message, viewer_id: int) -> MessageResponse:
    if message.sender_id == viewer_id:
        return MessageResponse(
            id=message.id,
            conversation_id=message.conversation_id,
            sender_id=message.sender_id,
            original_text=message.original_text,
            source_language=message.source_language,
            translated_text=message.original_text,
            target_language=message.source_language,
            created_at=message.created_at,
        )
    return MessageResponse.model_validate(message)


class TranslationSettingsResponse(BaseModel):
    provider: str
    api_url: str | None
    model: str | None
    system_prompt: str
    has_api_key: bool


class TranslationSettingsUpdate(BaseModel):
    provider: str = Field(pattern="^(local|openai_compatible)$")
    api_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    system_prompt: str = Field(min_length=1, max_length=4000)
