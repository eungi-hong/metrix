"""POST /chat -- grounded conversation over the stored movements and news."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, LLMDep, SessionDep
from app.core.errors import SpendCapReached, UpstreamError
from app.schemas.chat import ChatRequest, ChatResponse
from app.services import quotas
from app.services.chat import ConversationNotFound, answer_question
from app.services.quotas import Quota

router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Ask a question about a ticker's movements, answered from stored data",
)
async def chat(
    request: ChatRequest, session: SessionDep, llm: LLMDep, principal: CurrentUser
) -> ChatResponse:
    """Answer `question` from stored movements and linked news.

    Pass `conversation_id` from a previous response to continue a conversation;
    omit it to start one. Only your own conversations can be continued: any
    other id, including one that exists, is a 404. The ticker may be given
    explicitly, inferred from the question, or carried over from the
    conversation.

    Each turn counts against `chat_per_minute` and `chat_per_day`, and is
    given back if the answer fails on our side (the model is down, or the
    daily spend cap is reached).
    """
    if not request.question.strip():
        raise HTTPException(status_code=422, detail="`question` must not be blank.")
    await quotas.charge(principal, Quota.CHAT_PER_MINUTE)
    try:
        await quotas.charge(principal, Quota.CHAT_PER_DAY)
    except Exception:
        await quotas.refund(principal, Quota.CHAT_PER_MINUTE)
        raise
    try:
        return await answer_question(session, request, llm, principal)
    except ConversationNotFound:
        raise HTTPException(
            status_code=404, detail=f"No conversation with id {request.conversation_id}."
        ) from None
    except (UpstreamError, SpendCapReached):
        await quotas.refund(principal, Quota.CHAT_PER_MINUTE)
        await quotas.refund(principal, Quota.CHAT_PER_DAY)
        raise
