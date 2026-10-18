"""Request and response models for the chat endpoint."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

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
    """A movement the answer cites, with its citation label."""

    ref: str = Field(description="Citation label used in the answer: M and the movement id, e.g. 'M412'.")
    movement_id: int
    symbol: str | None = None
    date: date
    daily_return_pct: float
    direction: str


class ArticleSource(BaseModel):
    ref: str = Field(description="Citation label used in the answer: A and the article id, e.g. 'A1033'.")
    article_id: int
    title: str | None
    url: str
    source: str | None
    published_at: date | None
    relevance_tier: str | None = None
    relevance_score: float | None = None


class FilingSource(BaseModel):
    ref: str = Field(description="Citation label used in the answer: F and the filing id, e.g. 'F7'.")
    filing_id: int
    symbol: str | None = None
    accession: str
    form: str
    items: list[str] = Field(default_factory=list)
    url: str | None = None
    accepted_at: datetime | None = None


class ChatSources(BaseModel):
    """Only what the answer actually cites."""

    movements: list[MovementSource] = Field(default_factory=list)
    articles: list[ArticleSource] = Field(default_factory=list)
    filings: list[FilingSource] = Field(default_factory=list)


class ToolCallOut(BaseModel):
    """A database lookup the model made while answering."""

    name: str
    arguments: dict[str, Any]
    summary: str


class ChatResponse(BaseModel):
    conversation_id: str
    ticker: str | None = Field(description="The main ticker the answer is about, if any.")
    answer: str
    sources: ChatSources
    grounded: bool = Field(
        description="True when the answer cites stored evidence (a movement, article or filing "
        "label that a lookup returned); false otherwise, in which case it says what it could not find."
    )
    answered_by: Literal["model", "fallback"] = Field(
        default="model",
        description="'fallback': the model was unavailable or over the spend cap, and the answer is a "
        "summary assembled from the database, not written by a model.",
    )
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    citations_removed: int = Field(
        default=0, description="Citations of labels no lookup returned, removed from the answer."
    )
    suggestions: list[str] = Field(
        default_factory=list, description="Follow-up questions built from the data (not by a model)."
    )


class ErrorOut(BaseModel):
    """Uniform error body for every non-2xx response."""

    error: Literal[
        "not_found",
        "invalid_request",
        "upstream_unavailable",
        "configuration_error",
        "internal_error",
        "spend_cap_reached",
        "unauthorized",
        "rate_limited",
    ]
    detail: str


class ConversationSummary(BaseModel):
    id: str
    ticker: str | None
    created_at: datetime
    last_message_at: datetime | None
    messages: int = Field(description="Messages in the conversation, questions and answers.")


class ConversationList(BaseModel):
    conversations: list[ConversationSummary]
    limit: int
    offset: int
    total: int


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    sources: ChatSources | None = Field(
        default=None, description="For answers: what was in the prompt it was grounded in."
    )
    created_at: datetime


class ConversationDetail(BaseModel):
    id: str
    ticker: str | None
    created_at: datetime
    messages: list[MessageOut]
