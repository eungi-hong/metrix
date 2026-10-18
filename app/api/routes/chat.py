"""POST /chat and POST /chat/stream: grounded answers over stored data.

The two run the same turn (`app.services.chat.answer_question`). The stream
sends it as server-sent events while it happens:

    tool_call     {"name", "arguments"}             a database lookup starts
    tool_result   {"name", "summary", "rows"}       and what it found
    delta         {"text"}                          answer text as it is written
    done          the full ChatResponse             final answer, checked citations
    error         {"error", "detail"}               the turn failed

`delta` text is provisional: citations are checked after the answer is
written, so a client shows the deltas while they arrive and then replaces
them with `done.answer`. Text streamed before a `tool_call` was the model
thinking aloud before a lookup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, LLMDep, ProviderCredentialsDep, SessionDep, SessionFactoryDep
from app.core.errors import MetrixError, UpstreamError, WorkDeferred
from app.core.logging import get_logger
from app.models.chat import Conversation
from app.schemas.chat import ChatRequest, ChatResponse
from app.services import quotas
from app.services.chat import ConversationNotFound, answer_question, owns
from app.services.quotas import Quota

logger = get_logger(__name__)
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
    await _charge_chat(principal, charged)
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


async def _charge_chat(principal, charged: bool) -> None:
    if charged:
        await quotas.charge(principal, Quota.CHAT_PER_MINUTE)
        try:
            await quotas.charge(principal, Quota.CHAT_PER_DAY)
        except Exception:
            await quotas.refund(principal, Quota.CHAT_PER_MINUTE)
            raise


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"


@router.post(
    "/chat/stream",
    summary="The same as POST /chat, streamed as server-sent events",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Events: tool_call, tool_result, delta (provisional answer text), "
            "done (the full ChatResponse, with checked citations), error.",
        },
        404: {"description": "No such conversation (or not yours)."},
    },
)
async def chat_stream(
    request: ChatRequest,
    session: SessionDep,
    factory: SessionFactoryDep,
    llm: LLMDep,
    provider_credentials: ProviderCredentialsDep,
    principal: CurrentUser,
) -> StreamingResponse:
    if not request.question.strip():
        raise HTTPException(status_code=422, detail="`question` must not be blank.")
    if request.conversation_id:
        existing = await session.get(Conversation, request.conversation_id)
        if existing is None or not owns(existing, principal):
            raise HTTPException(status_code=404, detail=f"No conversation with id {request.conversation_id}.")
    await session.close()  # the turn runs in its own session, inside the stream
    charged = not provider_credentials.supplied_by_user
    await _charge_chat(principal, charged)

    async def events():
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def emit(event: str, data: dict) -> None:
            await queue.put(_frame(event, data))

        async def run() -> None:
            try:
                async with factory() as turn_session:
                    response = await answer_question(turn_session, request, llm, principal, emit=emit)
                if response.answered_by == "fallback":
                    await refund_chat(principal, charged)
                await queue.put(_frame("done", response.model_dump(mode="json")))
            except ConversationNotFound:
                await refund_chat(principal, charged)
                await queue.put(_frame("error", {"error": "not_found", "detail": "No such conversation."}))
            except (UpstreamError, WorkDeferred, MetrixError) as exc:
                await refund_chat(principal, charged)
                await queue.put(_frame("error", {"error": "upstream_unavailable", "detail": str(exc)[:300]}))
            except Exception:
                logger.exception("chat_stream_failed")
                await refund_chat(principal, charged)
                await queue.put(_frame("error", {"error": "internal_error", "detail": "The answer failed."}))
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while (frame := await queue.get()) is not None:
                yield frame
        finally:
            if not task.done():  # the client went away: stop the turn
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
