"""Request and response models for the chat endpoint."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    ticker: str | None = Field(
        default=None,
        max_length=16,
        description="Optional. If omitted, the ticker is inferred from the question "
        "or carried over from the conversation.",
    )
    conversation_id: str | None = Field(
        default=None,
        description="Omit to start a new conversation; pass the id returned by a "
        "previous call to continue one.",
    )
    start: date | None = Field(default=None, description="Restrict retrieval window.")
    end: date | None = None


class MovementSource(BaseModel):
    """A movement that was injected into the prompt, with its citation label."""

    ref: str = Field(description="Citation label used in the answer, e.g. 'M1'.")
    movement_id: int
    date: date
    daily_return_pct: float
    direction: str


class ArticleSource(BaseModel):
    ref: str = Field(description="Citation label used in the answer, e.g. 'A3'.")
    article_id: int
    title: str | None
    url: str
    source: str | None
    published_at: date | None
    relevance_tier: str
    relevance_score: float


class ChatSources(BaseModel):
    movements: list[MovementSource] = Field(default_factory=list)
    articles: list[ArticleSource] = Field(default_factory=list)


class ChatResponse(BaseModel):
    conversation_id: str
    ticker: str | None
    answer: str
    sources: ChatSources
    grounded: bool = Field(
        description="False when no stored movements matched, in which case the "
        "answer says so rather than speculating."
    )


class ErrorOut(BaseModel):
    """Uniform error body for every non-2xx response."""

    error: Literal[
        "not_found",
        "invalid_request",
        "upstream_unavailable",
        "configuration_error",
        "internal_error",
    ]
    detail: str
