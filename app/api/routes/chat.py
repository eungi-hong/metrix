"""POST /chat -- grounded conversation over the stored movements and news."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, LLMDep, ProviderCredentialsDep, SessionDep
from app.core.errors import UpstreamError, WorkDeferred
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
    request: ChatRequest,
    session: SessionDep,
    llm: LLMDep,
    provider_credentials: ProviderCredentialsDep,
    principal: CurrentUser,
) -> ChatResponse:
    """Answer `question` from stored movements and linked news.

    Pass `conversation_id` from a previous response to continue a conversation;
    omit it to start one. Only your own conversations can be continued: any
    other id, including one that exists, is a 404. The ticker may be given
    explicitly, inferred from the question, or carried over from the
    conversation.

    The model answers by looking things up in the database with tools, and
    every citation in the answer is checked against what those lookups
    returned. When the model is down, too busy or over the daily spend cap,
    the answer is a summary assembled from the database instead
    (`answered_by: "fallback"`), still 200.

    Each turn counts against `chat_per_minute` and `chat_per_day`, and is
    given back when the answer did not use the model. A turn made with the
    caller's own provider keys counts against neither: it spends nothing of
    ours.
    """
    if not request.question.strip():
        raise HTTPException(status_code=422, detail="`question` must not be blank.")
    charged = not provider_credentials.supplied_by_user
    if charged:
        await quotas.charge(principal, Quota.CHAT_PER_MINUTE)
        try:
            await quotas.charge(principal, Quota.CHAT_PER_DAY)
        except Exception:
            await quotas.refund(principal, Quota.CHAT_PER_MINUTE)
            raise
    try:
        response = await answer_question(session, request, llm, principal)
    except ConversationNotFound:
        raise HTTPException(
            status_code=404, detail=f"No conversation with id {request.conversation_id}."
        ) from None
    except (UpstreamError, WorkDeferred):
        await refund_chat(principal, charged)
        raise
    if response.answered_by == "fallback":
        # Answered from the database without a model: nothing of ours was spent.
        await refund_chat(principal, charged)
    return response


async def refund_chat(principal, charged: bool) -> None:
    if charged:
        await quotas.refund(principal, Quota.CHAT_PER_MINUTE)
        await quotas.refund(principal, Quota.CHAT_PER_DAY)
