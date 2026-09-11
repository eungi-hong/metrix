"""Grounded question answering over stored movements and news.

Retrieval-augmented, with the retrieval deliberately kept deterministic: the
corpus for a ticker is at most a few dozen movements and a few hundred
articles, so a vector index would add infrastructure without improving recall
over a straight query. What is retrieved:

* every movement in range as a one-line summary -- cheap, and it means a
  question about a specific date can be answered even when that day is not one
  of the largest moves;
* the most significant movements in full, with their linked articles,
  rationales and tiers.

Each retrieved item gets a citation label (`M1`, `A3`) that the model is
required to cite and that is echoed back in `sources`, so every claim in an
answer can be traced to a row in the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.models.chat import ChatMessage, Conversation
from app.models.enums import MessageRole
from app.models.market import Movement, Ticker
from app.models.news import MovementNewsLink
from app.schemas.chat import (
    ArticleSource,
    ChatRequest,
    ChatResponse,
    ChatSources,
    MovementSource,
)
from app.services.llm import LLMClient

logger = get_logger(__name__)

_SYSTEM = """\
You answer questions about why a stock moved, using ONLY the movement and \
article data provided in the user message.

Rules:
- Ground every factual claim in the provided context and cite it inline with \
its label, like [M2] for a movement or [A5] for an article.
- If the context does not answer the question, say so plainly and describe what \
the data does show. Never invent a cause, a headline, a date, or a number.
- The articles were scored for how well they explain each move. Higher-scoring \
articles are better explanations; say when the evidence is weak.
- Distinguish the tiers when it matters: company-specific news (easy), \
competitor or industry news (medium), and macro or political news (hard).
- This dataset contains only days flagged as unusually large moves, and only \
news that was found and scored for them. It is not a complete news archive; do \
not imply otherwise.
- Be concise and concrete. Lead with the answer. No preamble."""


@dataclass(slots=True)
class RetrievedContext:
    """What retrieval found, ready to be rendered into a prompt."""

    ticker: Ticker | None = None
    movements: list[Movement] = field(default_factory=list)
    detailed: list[Movement] = field(default_factory=list)
    movement_refs: dict[int, str] = field(default_factory=dict)
    article_refs: dict[int, str] = field(default_factory=dict)
    articles: list[MovementNewsLink] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return bool(self.movements)


async def answer_question(
    session: AsyncSession, request: ChatRequest, llm: LLMClient
) -> ChatResponse:
    """Retrieve, prompt, answer, and persist one conversation turn."""
    conversation = await _load_or_create_conversation(session, request.conversation_id)

    ticker = await _resolve_ticker(session, request, conversation)
    context = await _retrieve(session, ticker, request.start, request.end)

    history = await _load_history(session, conversation)
    messages = history + [
        {"role": "user", "content": _render_prompt(request.question, context)}
    ]

    answer = await llm.complete(system=_SYSTEM, messages=messages)
    sources = _build_sources(context)

    session.add(
        ChatMessage(
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=request.question,
        )
    )
    session.add(
        ChatMessage(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=answer,
            sources=sources.model_dump(mode="json"),
        )
    )
    if ticker is not None:
        conversation.ticker_id = ticker.id
    await session.commit()

    logger.info(
        "chat_answered",
        conversation_id=conversation.id,
        symbol=ticker.symbol if ticker else None,
        movements=len(context.movements),
        articles=len(context.articles),
        grounded=context.grounded,
    )
    return ChatResponse(
        conversation_id=conversation.id,
        ticker=ticker.symbol if ticker else None,
        answer=answer,
        sources=sources,
        grounded=context.grounded,
    )


# ---------------------------------------------------------------- retrieval


async def _resolve_ticker(
    session: AsyncSession, request: ChatRequest, conversation: Conversation
) -> Ticker | None:
    """Explicit ticker, else the conversation's, else inferred from the question.

    Inference matches against tickers already in the database rather than
    asking the LLM: it is free, deterministic, and cannot hallucinate a symbol
    we have no data for.
    """
    if request.ticker:
        return await session.scalar(
            sa.select(Ticker).where(Ticker.symbol == request.ticker.strip().upper())
        )

    if conversation.ticker_id is not None:
        carried = await session.get(Ticker, conversation.ticker_id)
        if carried is not None:
            return carried

    return await _infer_ticker(session, request.question)


async def _infer_ticker(session: AsyncSession, question: str) -> Ticker | None:
    candidates = (await session.scalars(sa.select(Ticker))).all()
    if not candidates:
        return None

    tokens = set(re.findall(r"\b[A-Z]{1,6}\b", question))
    for ticker in candidates:
        if ticker.symbol in tokens:
            return ticker

    lowered = question.lower()
    # Longest name first, so "Alphabet Inc" wins over a shorter contained name.
    named = sorted(
        (t for t in candidates if t.company_name),
        key=lambda t: len(t.company_name or ""),
        reverse=True,
    )
    for ticker in named:
        if _company_alias(ticker.company_name or "") in lowered:
            return ticker
    return None


def _company_alias(company_name: str) -> str:
    """Strip corporate suffixes so 'Apple Inc.' matches a question about 'Apple'."""
    alias = re.sub(
        r"\b(inc|incorporated|corp|corporation|co|ltd|plc|sa|nv|ag|holdings|group)\b\.?",
        "",
        company_name,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^a-z0-9 ]", "", alias.lower()).strip()


async def _retrieve(
    session: AsyncSession, ticker: Ticker | None, start: date | None, end: date | None
) -> RetrievedContext:
    if ticker is None:
        return RetrievedContext()

    conditions = [Movement.ticker_id == ticker.id]
    if start:
        conditions.append(Movement.date >= start)
    if end:
        conditions.append(Movement.date <= end)

    movements = list(
        (
            await session.scalars(
                sa.select(Movement)
                .where(*conditions)
                .order_by(Movement.date.desc())
                .limit(settings.chat_max_movement_summaries)
                .options(
                    selectinload(Movement.news_links).selectinload(
                        MovementNewsLink.article
                    )
                )
            )
        ).all()
    )
    if not movements:
        return RetrievedContext(ticker=ticker)

    detailed = sorted(movements, key=lambda m: m.abs_return, reverse=True)[
        : settings.chat_max_movements_in_context
    ]
    detailed.sort(key=lambda m: m.date, reverse=True)

    context = RetrievedContext(ticker=ticker, movements=movements, detailed=detailed)
    for i, movement in enumerate(movements, start=1):
        context.movement_refs[movement.id] = f"M{i}"

    article_index = 1
    for movement in detailed:
        for link in _top_links(movement):
            if link.article_id in context.article_refs:
                continue
            context.article_refs[link.article_id] = f"A{article_index}"
            context.articles.append(link)
            article_index += 1
    return context


def _top_links(movement: Movement) -> list[MovementNewsLink]:
    return sorted(movement.news_links, key=lambda l: l.relevance_score, reverse=True)[
        : settings.chat_max_articles_per_movement
    ]


# ------------------------------------------------------------------ prompting


def _render_prompt(question: str, context: RetrievedContext) -> str:
    if context.ticker is None:
        return (
            f"QUESTION: {question}\n\n"
            "CONTEXT: No ticker could be identified from this question, and no "
            "movement data is available. Tell the user to name a ticker symbol."
        )
    if not context.movements:
        return (
            f"QUESTION: {question}\n\n"
            f"CONTEXT: {context.ticker.symbol} is tracked, but no major movements "
            "are stored for the requested period. Say so."
        )

    lines = [
        f"COMPANY: {context.ticker.company_name or context.ticker.symbol} "
        f"({context.ticker.symbol})",
        f"SECTOR: {context.ticker.sector or 'unknown'} / "
        f"{context.ticker.industry or 'unknown'}",
        "",
        f"ALL MAJOR MOVEMENTS ON RECORD ({len(context.movements)}):",
    ]
    for movement in context.movements:
        ref = context.movement_refs[movement.id]
        lines.append(
            f"  [{ref}] {movement.date.isoformat()}  {movement.daily_return:+.2%}  "
            f"({movement.direction.value}, threshold {movement.threshold:.2%} via "
            f"{movement.threshold_source}, {len(movement.news_links)} linked article(s))"
        )

    lines += ["", "NEWS LINKED TO THE MOST SIGNIFICANT MOVEMENTS:"]
    for movement in context.detailed:
        ref = context.movement_refs[movement.id]
        links = _top_links(movement)
        lines.append(
            f"\n  [{ref}] {movement.date.isoformat()} {movement.daily_return:+.2%}"
        )
        if not links:
            lines.append("      (no explanatory news was found for this movement)")
            continue
        for link in links:
            article = link.article
            lines.append(
                f"      [{context.article_refs[link.article_id]}] "
                f"({link.relevance_tier.value}, score {link.relevance_score:.2f}) "
                f"{article.title or '(untitled)'} -- {article.source_domain or 'unknown'}"
                f", {article.published_at.date().isoformat() if article.published_at else 'date unknown'}"
            )
            if link.rationale:
                lines.append(f"          why: {link.rationale}")

    lines += ["", f"QUESTION: {question}"]
    return "\n".join(lines)


def _build_sources(context: RetrievedContext) -> ChatSources:
    return ChatSources(
        movements=[
            MovementSource(
                ref=context.movement_refs[m.id],
                movement_id=m.id,
                date=m.date,
                daily_return_pct=round(m.daily_return * 100, 4),
                direction=m.direction.value,
            )
            for m in context.movements
        ],
        articles=[
            ArticleSource(
                ref=context.article_refs[link.article_id],
                article_id=link.article_id,
                title=link.article.title,
                url=link.article.url,
                source=link.article.source_domain,
                published_at=link.article.published_at.date()
                if link.article.published_at
                else None,
                relevance_tier=link.relevance_tier.value,
                relevance_score=link.relevance_score,
            )
            for link in context.articles
        ],
    )


# --------------------------------------------------------------- persistence


async def _load_or_create_conversation(
    session: AsyncSession, conversation_id: str | None
) -> Conversation:
    if conversation_id:
        existing = await session.get(Conversation, conversation_id)
        if existing is not None:
            return existing
    conversation = Conversation()
    session.add(conversation)
    await session.flush()
    return conversation


async def _load_history(
    session: AsyncSession, conversation: Conversation
) -> list[dict[str, str]]:
    """Prior turns, oldest first.

    Only the user's raw questions are replayed, not the retrieved context that
    was attached to them -- re-sending several turns of full article text would
    blow up the prompt for no benefit, since the current turn's retrieval is
    already the freshest view of the same data.
    """
    rows = (
        await session.scalars(
            sa.select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.id.desc())
            .limit(settings.chat_max_history_messages)
        )
    ).all()
    return [{"role": m.role.value, "content": m.content} for m in reversed(rows)]
