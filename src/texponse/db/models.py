from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class ConversationEventType(StrEnum):
    CALL_CREATED = "call.created"
    CALL_ANSWRED = "call.answered"
    CALL_ENDED = "call.ended"
    CALL_HUNGUP = "call.hungup"
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SENT = "message.sent"
    MESSAGE_STATUS_UPDATED = "message.status_updated"
    WEBHOOK_RECEIVED = "webhook.received"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    ESCALATED = "escalated"


class UserRole(StrEnum):
    ADMIN = "admin"
    AGENT = "agent"
    VIEWER = "viewer"
    SYSTEM = "system"


class ConversationEventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    phone_number: str | None = Field(default=None, index=True)
    company_id: str = Field(index=True)
    role: UserRole = Field(default=UserRole.VIEWER)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class JobberToken(SQLModel, table=True):
    __tablename__ = "jobber_tokens"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    company_id: str = Field(unique=True, index=True)
    access_token: str
    refresh_token: str
    expires_at: datetime
    token_type: str = Field(default="Bearer")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    aircall_id: str = Field(index=True)
    company_id: str = Field(index=True)
    direction: str = Field()
    channel: str = Field()
    status: ConversationStatus = Field(default=ConversationStatus.ACTIVE)
    assigned_agent_id: str | None = Field(default=None, foreign_key="users.id")
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ConversationEvent(SQLModel, table=True):
    __tablename__ = "conversation_events"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    conversation_id: str = Field(foreign_key="conversations.id", index=True)
    event_type: ConversationEventType = Field(index=True)
    status: ConversationEventStatus = Field(default=ConversationEventStatus.PENDING)
    aircall_call_id: str | None = Field(default=None, index=True)
    aircall_message_id: str | None = Field(default=None, index=True)
    from_number: str | None = Field(default=None, index=True)
    to_number: str | None = Field(default=None, index=True)
    content: str | None = Field(default=None)
    direction: str | None = Field(default=None)
    raw_payload: dict[str, Any] = Field(sa_column=Column("raw_payload", JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Response(SQLModel, table=True):
    __tablename__ = "responses"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    conversation_id: str = Field(foreign_key="conversations.id", index=True)
    content: str
    model_used: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    retrieved_passage_count: int = 0
    was_fallback: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
