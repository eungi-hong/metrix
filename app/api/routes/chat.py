"""POST /chat -- grounded conversation over the stored movements and news."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import LLMDep, SessionDep
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat import answer_question

router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Ask a question about a ticker's movements, answered from stored data",
)
async def chat(request: ChatRequest, session: SessionDep, llm: LLMDep) -> ChatResponse:
    """Answer `question` from stored movements and linked news.

    Pass `conversation_id` from a previous response to continue a conversation;
    omit it to start one. The ticker may be given explicitly, inferred from the
    question, or carried over from the conversation.
    """
    if not request.question.strip():
        raise HTTPException(status_code=422, detail="`question` must not be blank.")
    return await answer_question(session, request, llm)
