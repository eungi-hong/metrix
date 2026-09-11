"""Conversation state for the chat endpoint.

Persisting turns (rather than making the client echo history back) keeps the
server the source of truth for what was actually grounded in what, and lets a
conversation be resumed or audited later.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin
from app.models.enums import MessageRole, sa_enum


def new_conversation_id() -> str:
    return str(uuid.uuid4())


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(
        sa.String(36), primary_key=True, default=new_conversation_id
    )
    ticker_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("tickers.id", ondelete="SET NULL")
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
        lazy="noload",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (sa.Index("ix_chat_conversation", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        sa_enum(MessageRole, "message_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Which movements / articles were injected into the prompt for this answer.
    sources: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(
        back_populates="messages", lazy="noload"
    )
