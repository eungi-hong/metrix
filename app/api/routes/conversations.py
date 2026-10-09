"""GET /conversations -- a caller's own chat history.

Someone else's conversation answers 404, never 403, so its existence is not
revealed. Conversations from before ownership belong to no one and are
readable with a valid X-Admin-Token only.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query

from app.api.deps import AdminView, CurrentUser, SessionDep
from app.models.chat import ChatMessage, Conversation
from app.models.market import Ticker
from app.schemas.chat import (
    ChatSources,
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    MessageOut,
)
from app.services.auth import Principal
from app.services.chat import owns

router = APIRouter(tags=["chat"])


def _owned_by(principal: Principal) -> sa.ColumnElement[bool]:
    if principal.anonymous:
        return Conversation.anonymous_key == principal.key
    return Conversation.user_id == principal.user_id


@router.get(
    "/conversations",
    response_model=ConversationList,
    summary="Your conversations, most recently active first",
)
async def list_conversations(
    session: SessionDep,
    principal: CurrentUser,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ConversationList:
    stats = (
        sa.select(
            ChatMessage.conversation_id,
            sa.func.count().label("messages"),
            sa.func.max(ChatMessage.created_at).label("last_message_at"),
            # Ids only grow, so the newest message has the largest; a clock
            # can tie (SQLite's has one-second resolution) or step back.
            sa.func.max(ChatMessage.id).label("last_message_id"),
        )
        .group_by(ChatMessage.conversation_id)
        .subquery()
    )
    owned = _owned_by(principal)
    total = await session.scalar(
        sa.select(sa.func.count()).select_from(Conversation).where(owned)
    )
    rows = (
        await session.execute(
            sa.select(Conversation, Ticker.symbol, stats.c.messages, stats.c.last_message_at)
            .outerjoin(stats, stats.c.conversation_id == Conversation.id)
            .outerjoin(Ticker, Ticker.id == Conversation.ticker_id)
            .where(owned)
            .order_by(
                sa.func.coalesce(stats.c.last_message_id, 0).desc(),
                Conversation.created_at.desc(),
                Conversation.id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ConversationList(
        conversations=[
            ConversationSummary(
                id=conversation.id,
                ticker=symbol,
                created_at=conversation.created_at,
                last_message_at=last_message_at,
                messages=messages or 0,
            )
            for conversation, symbol, messages, last_message_at in rows
        ],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="One of your conversations, with every message and its sources",
)
async def get_conversation(
    conversation_id: str, session: SessionDep, principal: CurrentUser, admin: AdminView
) -> ConversationDetail:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or not (admin or owns(conversation, principal)):
        raise HTTPException(status_code=404, detail=f"No conversation with id {conversation_id}.")
    ticker = (
        await session.get(Ticker, conversation.ticker_id) if conversation.ticker_id else None
    )
    messages = (
        await session.scalars(
            sa.select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.id)
        )
    ).all()
    return ConversationDetail(
        id=conversation.id,
        ticker=ticker.symbol if ticker else None,
        created_at=conversation.created_at,
        messages=[
            MessageOut(
                id=message.id,
                role=message.role.value,
                content=message.content,
                sources=ChatSources.model_validate(message.sources) if message.sources else None,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )
