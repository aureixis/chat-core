from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_picture_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    preferred_language: Mapped[str] = mapped_column(String(20), default="en")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("requester_id", "recipient_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("user_a_id", "user_b_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_a_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    user_b_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    original_text: Mapped[str] = mapped_column(Text)
    source_language: Mapped[str] = mapped_column(String(20))
    translated_text: Mapped[str] = mapped_column(Text)
    target_language: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TranslationSettings(Base):
    __tablename__ = "translation_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(40), default="local")
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
