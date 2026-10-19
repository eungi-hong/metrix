"""Ask Metrix: grounded, tool-using answers over stored movements and evidence.

One question is one bounded loop:

1. **Resolve tickers** the question is about: the explicit `ticker`, else
   symbols and company names it mentions (an indexed lookup, so "did AMD and
   NVDA fall for the same reason?" finds both), else the conversation's.
2. **Run the model with tools** (`app.services.chat_tools`): at most
   CHAT_MAX_TOOL_CALLS calls, CHAT_TURN_TOKEN_BUDGET tokens, and the spend
   cap checked before every model call (each call goes through the metered
   provider seam). The system prompt and tool definitions are the same on
   every turn and come first, so provider prompt caching applies; anything
   specific to this question goes in the user message.
3. **Check the citations.** Every tool row carries a label (M, A, F and an
   id). A cited label the tools did not return this turn is stripped and
   logged. Only cited rows are returned as `sources`, and `grounded` means the
   answer cites evidence, not that rows were retrieved.
4. **Fall back** when the model is unavailable, failing, too busy or over the
   spend cap: a deterministic summary from the database (each ticker's
   largest moves and their strongest evidence), marked as not model-written.
5. **Suggest follow-ups** from the data with templates, not the model.

`answer_question` takes an `emit` callback for the streaming endpoint:
`delta` (answer text as it is written), `tool_call`, `tool_result`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    ConfigurationError,
    LLMError,
    SpendCapReached,
    UpstreamError,
    WorkDeferred,
)
from app.core.logging import get_logger
from app.core.symbols import normalize_symbol
from app.models.chat import ChatMessage, Conversation
from app.models.enums import Direction, MessageRole
from app.models.filings import Filing
from app.models.market import Movement, PriceBar, Ticker
from app.schemas.chat import ChatRequest, ChatResponse, ChatSources, ToolCallOut
from app.services import chat_tools, demand, symbol_directory
from app.services.auth import Principal
from app.services.llm import LLMProvider
from app.services.llm.base import ToolCall

logger = get_logger(__name__)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

SYSTEM = """\
You are Ask Metrix. You answer questions about why stocks moved, using only \
the Metrix database, which you read with the tools provided. You have no \
other knowledge of prices or news: never state a price move, date, headline \
or cause that a tool did not return.

How to work:
- Look things up before answering. Start with get_movements for the tickers \
in question; open get_movement_case for the specific days that matter; use \
search_evidence for themes across days or tickers; get_filings for SEC \
filings; compare for performance against the market over a period.
- For questions about several tickers, look each one up.
- Make as few calls as the question needs.

How to answer:
- Every row the tools return has a label such as M412 (a movement), A1033 (an \
article) or F7 (an SEC filing). Cite the label for every fact, in its own \
square brackets, right after the claim: "fell 6.2% on 2026-06-05 [M412] after \
an export ban was reported [A1033]". Never cite a label a tool did not return, \
and never invent one.
- Every number you state must appear in a tool result. Round as the tool did.
- Use the decomposition: say whether a move was mostly market, sector or \
company-specific. Say when evidence is weak (low score, context role, timing \
after the close) or missing.
- The database holds only days flagged as unusually large moves and the \
evidence found for them. It is not a full news archive; do not imply it is.
- If the tools cannot answer the question, say so and say what the data does \
show.
- Be concise and concrete. Lead with the answer. No preamble."""

FALLBACK_HEADER = (
    "Summary from stored data. The language model was unavailable, so this answer was assembled "
    "from the database and not written by a model."
)
# A bracket group whose every part looks like a label: [M12], [A3, F4], [M1; A2].
_CITATION_GROUP = re.compile(r"\s?\[\s*([MAF]\d+(?:\s*[,;]\s*[MAF]\d+)*)\s*\]")
_LABEL = re.compile(r"[MAF]\d+")
_COMMON_WORDS = {
    "A", "I", "AM", "AN", "AND", "ARE", "AS", "AT", "BE", "BY", "DID", "DO", "FOR", "HOW", "IF", "IN",
    "IS", "IT", "ITS", "ME", "MY", "NO", "NOT", "OF", "ON", "OR", "SO", "THE", "TO", "UP", "US", "WAS",
    "WE", "WHAT", "WHY", "YOU", "ETF", "CEO", "CFO", "EPS", "IPO", "SEC", "USA", "AI", "Q", "YOY",
}
_NAME_STOPWORDS = {
    "what", "why", "did", "does", "the", "and", "for", "with", "this", "that", "year", "week", "month",
    "today", "stock", "stocks", "shares", "move", "moved", "fall", "fell", "rise", "rose", "drop",
    "dropped", "happened", "same", "reason", "company", "market", "news",
}


class ConversationNotFound(LookupError):
    pass


def fallback_reason(exc: BaseException) -> str:
    """Why chat answered from the database, in terms a visitor can act on.

    Only the category leaves the server: the provider's own message can name
    accounts or limits, so it stays in the log.
    """
    if isinstance(exc, LLMError) and exc.out_of_credit:
        return "out_of_credit"
    if isinstance(exc, SpendCapReached):
        return "spend_cap"
    if isinstance(exc, WorkDeferred):
        return "busy"
    if isinstance(exc, ConfigurationError) or (isinstance(exc, LLMError) and exc.permanent):
        return "not_configured"
    return "unavailable"


@dataclass(slots=True)
class LoopResult:
    text: str
    tool_calls: list[ToolCallOut]
    answered_by: str  # "model" or "fallback"
    fallback_reason: str | None = None


async def answer_question(
    session: AsyncSession,
    request: ChatRequest,
    llm: LLMProvider,
    principal: Principal,
    *,
    emit: Emit | None = None,
) -> ChatResponse:
    """Resolve, look up, answer, check citations, and persist one turn.

    Raises `ConversationNotFound` for a `conversation_id` the caller does not
    own, exactly as for one that does not exist.
    """
    conversation = await _load_or_create_conversation(session, request.conversation_id, principal)
    tickers = await resolve_tickers(session, request, conversation)
    for ticker in tickers[:3]:
        # Persisted with this turn's commit; a turn that fails does not count.
        await demand.count_request(session, ticker.symbol, principal)

    turn = chat_tools.Turn(session=session, principal=principal)
    history = await _load_history(session, conversation)
    user = await _render_user_message(session, request, tickers)

    try:
        if not llm.available:
            raise ConfigurationError("no language model is configured")
        result = await _run_loop(llm, turn, history + [{"role": "user", "content": user}], emit)
    except (UpstreamError, ConfigurationError, WorkDeferred) as exc:
        reason = fallback_reason(exc)
        logger.warning(
            "chat_fallback", reason=reason, error=type(exc).__name__, detail=str(exc)[:200]
        )
        turn = chat_tools.Turn(session=session, principal=principal)  # only what the summary cites
        text = await fallback_answer(turn, tickers, request.start, request.end)
        result = LoopResult(text=text, tool_calls=[], answered_by="fallback", fallback_reason=reason)

    answer, cited, stripped = check_citations(result.text, turn.labels)
    if stripped:
        logger.warning("chat_citations_stripped", labels=stripped, conversation_id=conversation.id)
    sources = ChatSources(
        movements=[turn.movements[label] for label in cited if label in turn.movements],
        articles=[turn.articles[label] for label in cited if label in turn.articles],
        filings=[turn.filings[label] for label in cited if label in turn.filings],
    )
    symbols = [t.symbol for t in tickers] or turn.symbols
    suggestions = await suggest_follow_ups(session, symbols, request.question)

    session.add(ChatMessage(conversation_id=conversation.id, role=MessageRole.USER, content=request.question))
    session.add(
        ChatMessage(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=answer,
            sources=sources.model_dump(mode="json"),
        )
    )
    primary = tickers[0] if tickers else None
    if primary is None and turn.symbols:
        primary = await session.scalar(sa.select(Ticker).where(Ticker.symbol == turn.symbols[0]))
    if primary is not None:
        conversation.ticker_id = primary.id
    await session.commit()

    logger.info(
        "chat_answered",
        conversation_id=conversation.id,
        symbols=symbols,
        answered_by=result.answered_by,
        tool_calls=len(result.tool_calls),
        cited=len(cited),
        stripped=len(stripped),
    )
    return ChatResponse(
        conversation_id=conversation.id,
        ticker=primary.symbol if primary else None,
        answer=answer,
        sources=sources,
        grounded=bool(cited),
        answered_by=result.answered_by,
        fallback_reason=result.fallback_reason,
        tool_calls=result.tool_calls,
        citations_removed=len(stripped),
        suggestions=suggestions,
    )


# --------------------------------------------------------------- the loop


async def _run_loop(llm: LLMProvider, turn: chat_tools.Turn, messages: list[dict[str, Any]], emit: Emit | None) -> LoopResult:
    calls_out: list[ToolCallOut] = []
    used_calls = 0
    used_tokens = 0

    async def on_text(text: str) -> None:
        if emit is not None:
            await emit("delta", {"text": text})

    while True:
        tools_allowed = used_calls < settings.chat_max_tool_calls and used_tokens < settings.chat_turn_token_budget
        step = await llm.tool_turn(
            system=SYSTEM,
            messages=messages,
            tools=chat_tools.TOOLS if tools_allowed else [],
            operation="chat",
            max_tokens=settings.chat_max_output_tokens,
            on_text=on_text,
        )
        used_tokens += step.input_tokens + step.output_tokens
        if not step.tool_calls or not tools_allowed:
            if not step.text.strip():
                raise LLMError(getattr(llm, "name", "llm"), "the model returned no answer")
            return LoopResult(text=step.text.strip(), tool_calls=calls_out, answered_by="model")

        messages.append({"role": "assistant", "content": step.text, "tool_calls": step.tool_calls})
        for call in step.tool_calls:
            if used_calls >= settings.chat_max_tool_calls:
                content = '{"error": "tool call limit reached for this question; answer with what you have"}'
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
                continue
            used_calls += 1
            if emit is not None:
                await emit("tool_call", {"name": call.name, "arguments": call.arguments})
            result = await chat_tools.run(turn, call)
            calls_out.append(ToolCallOut(name=call.name, arguments=call.arguments, summary=result.summary))
            if emit is not None:
                await emit("tool_result", {"name": call.name, "summary": result.summary, "rows": result.rows})
            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result.content})


def check_citations(text: str, valid: set[str]) -> tuple[str, list[str], list[str]]:
    """Keep only citations of labels the tools returned this turn.

    Returns the cleaned text, the labels it cites (in first-cited order) and
    the labels that were stripped. A bracket group left with no valid label is
    removed; one with some is rewritten to those.
    """
    cited: list[str] = []
    stripped: list[str] = []

    def replace(match: re.Match[str]) -> str:
        labels = _LABEL.findall(match.group(1))
        kept = [label for label in labels if label in valid]
        stripped.extend(label for label in labels if label not in valid)
        for label in kept:
            if label not in cited:
                cited.append(label)
        if not kept:
            return ""
        leading = " " if match.group(0).startswith(" ") else ""
        return leading + "".join(f"[{label}]" for label in kept)

    cleaned = _CITATION_GROUP.sub(replace, text)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return cleaned.strip(), cited, stripped


# ------------------------------------------------------------- fallback


async def fallback_answer(
    turn: chat_tools.Turn, tickers: list[Ticker], start: date | None, end: date | None
) -> str:
    """Each ticker's largest moves and their strongest evidence, from the
    database, cited like a model answer (through the same tools)."""
    if not tickers:
        return FALLBACK_HEADER + " Name a ticker, for example NVDA, to see its largest stored moves."
    lines = [FALLBACK_HEADER]
    for ticker in tickers[:3]:
        arguments: dict[str, Any] = {"symbol": ticker.symbol, "sort": "abs_return", "limit": 3}
        if start:
            arguments["start"] = start.isoformat()
        if end:
            arguments["end"] = end.isoformat()
        result = await chat_tools.run(turn, ToolCall("fallback", "get_movements", arguments))
        body = json.loads(result.content)
        moves = body.get("movements") or []
        if not moves:
            lines.append(f"\n{ticker.symbol}: no flagged movements are stored for this period.")
            continue
        lines.append(f"\nLargest stored moves for {ticker.symbol}:")
        for move in moves:
            line = f"- {move['date']}: {move['return_pct']:+.2f}% [{move['label']}]."
            if move.get("dominant"):
                line += f" Mostly {move['dominant']}-driven."
            top = move.get("top_evidence")
            if top:
                line += f" Strongest evidence: {top['title'] or 'an article'} [{top['label']}]"
                if top.get("rationale"):
                    line += f" ({top['rationale'].rstrip('.')})"
                line += "."
            else:
                line += f" No explanatory evidence is stored yet ({move['evidence'].replace('_', ' ')})."
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------- suggestions


async def suggest_follow_ups(session: AsyncSession, symbols: list[str], question: str, limit: int = 3) -> list[str]:
    """Follow-up questions from the data, by template."""
    if not symbols:
        return []
    out: list[str] = []
    main = symbols[0]
    ticker = await session.scalar(sa.select(Ticker).where(Ticker.symbol == main))
    if ticker is None:
        return []
    base = [Movement.ticker_id == ticker.id, Movement.qualifies.is_(True)]
    down = await session.scalar(
        sa.select(Movement).where(*base, Movement.direction == Direction.DOWN).order_by(Movement.abs_return.desc()).limit(1)
    )
    up = await session.scalar(
        sa.select(Movement).where(*base, Movement.direction == Direction.UP).order_by(Movement.abs_return.desc()).limit(1)
    )
    if down is not None:
        out.append(f"Why did {main} fall {down.abs_return * 100:.1f}% on {down.date.isoformat()}?")
    if up is not None:
        out.append(f"What drove {main}'s {up.abs_return * 100:.1f}% rise on {up.date.isoformat()}?")
    if len(symbols) > 1:
        out.append(f"Did {main} and {symbols[1]} move for the same reasons this year?")
    elif await session.scalar(sa.select(Filing.id).where(Filing.ticker_id == ticker.id).limit(1)):
        out.append(f"Which SEC filings came out around {main}'s biggest moves?")
    if await session.scalar(sa.select(PriceBar.id).where(PriceBar.ticker_id == ticker.id).limit(1)):
        out.append(f"How did {main} do against the market over the last 3 months?")
    asked = question.strip().lower()
    return [q for q in dict.fromkeys(out) if q.lower() != asked][:limit]


# ---------------------------------------------------------- resolution


async def resolve_tickers(session: AsyncSession, request: ChatRequest, conversation: Conversation) -> list[Ticker]:
    """The explicit ticker; else every held ticker the question names; else the
    conversation's. Only tickers Metrix has researched."""
    if request.ticker:
        symbol = symbol_directory.canonical(normalize_symbol(request.ticker))
        ticker = await session.scalar(sa.select(Ticker).where(Ticker.symbol == symbol))
        return [ticker] if ticker is not None else []
    mentioned = await infer_tickers(session, request.question)
    if mentioned:
        return mentioned
    if conversation.ticker_id is not None:
        carried = await session.get(Ticker, conversation.ticker_id)
        if carried is not None:
            return [carried]
    return []


async def infer_tickers(session: AsyncSession, question: str) -> list[Ticker]:
    """Symbols and company names in the question, by indexed lookups: the
    symbol's unique index, and company-name prefixes (a trigram index on
    Postgres), never a scan of every ticker. In the order they appear."""
    found: dict[str, tuple[int, Ticker]] = {}
    symbol_positions: dict[str, int] = {}
    for match in re.finditer(r"\$([A-Za-z]{1,6}(?:[.-][A-Za-z])?)\b|\b([A-Z]{1,6}(?:[.-][A-Z])?)\b", question):
        raw = match.group(1) or match.group(2)
        if match.group(2) and raw in _COMMON_WORDS:
            continue
        symbol_positions.setdefault(symbol_directory.canonical(normalize_symbol(raw)), match.start())
    if symbol_positions:
        for ticker in (
            await session.scalars(
                sa.select(Ticker).where(Ticker.symbol.in_(list(symbol_positions)), Ticker.last_ingested_at.is_not(None))
            )
        ).all():
            found[ticker.symbol] = (symbol_positions[ticker.symbol], ticker)

    lowered = question.lower()
    words = []
    for match in re.finditer(r"[A-Za-z][A-Za-z&'-]{2,}", question):
        word = match.group(0).lower()
        if word not in _NAME_STOPWORDS and word not in words:
            words.append(word)
    if words:
        name = sa.func.lower(Ticker.company_name)
        candidates = (
            await session.scalars(
                sa.select(Ticker).where(
                    Ticker.last_ingested_at.is_not(None),
                    sa.or_(*[name.like(f"{w}%") for w in words[:8]]),
                ).limit(20)
            )
        ).all()
        for ticker in candidates:
            alias = _company_alias(ticker.company_name or "")
            position = lowered.find(alias) if alias else -1
            if position >= 0 and ticker.symbol not in found:
                found[ticker.symbol] = (position, ticker)
    return [ticker for _, ticker in sorted(found.values(), key=lambda item: item[0])]


def _company_alias(company_name: str) -> str:
    """Strip corporate suffixes so 'Apple Inc.' matches a question about 'Apple'."""
    alias = re.sub(
        r"\b(inc|incorporated|corp|corporation|co|ltd|plc|sa|nv|ag|holdings|group)\b\.?",
        "",
        company_name,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", alias.lower())).strip()


async def _render_user_message(session: AsyncSession, request: ChatRequest, tickers: list[Ticker]) -> str:
    """The question, with what is known about it. Kept out of the system
    prompt so that prompt stays identical, and cacheable, on every turn."""
    lines = []
    if tickers:
        described = []
        for ticker in tickers[:5]:
            first, last, count = (
                await session.execute(
                    sa.select(sa.func.min(Movement.date), sa.func.max(Movement.date), sa.func.count())
                    .where(Movement.ticker_id == ticker.id, Movement.qualifies.is_(True))
                )
            ).one()
            span = f"{count} flagged movements, {first} to {last}" if count else "no flagged movements stored"
            described.append(f"{ticker.symbol} ({ticker.company_name or 'unknown company'}; {span})")
        lines.append("TICKERS: " + ", ".join(t.symbol for t in tickers[:5]))
        lines.append("ABOUT THEM: " + "; ".join(described))
    else:
        lines.append("TICKERS: none identified. If the question names a company, try its symbol with the tools.")
    if request.start or request.end:
        lines.append(f"PERIOD: {request.start or 'any'} to {request.end or 'any'} (pass these to the tools)")
    lines.append(f"QUESTION: {request.question}")
    return "\n".join(lines)


# --------------------------------------------------------- persistence


def owns(conversation: Conversation, principal: Principal) -> bool:
    """Whether `principal` may read and continue `conversation`.

    One owned by neither a user nor an anonymous caller predates ownership:
    no caller owns it, and only an admin may read it.
    """
    if conversation.user_id is not None:
        return conversation.user_id == principal.user_id
    if conversation.anonymous_key is not None:
        return principal.anonymous and conversation.anonymous_key == principal.key
    return False


async def _load_or_create_conversation(
    session: AsyncSession, conversation_id: str | None, principal: Principal
) -> Conversation:
    if conversation_id:
        existing = await session.get(Conversation, conversation_id)
        if existing is None or not owns(existing, principal):
            raise ConversationNotFound(conversation_id)
        return existing
    conversation = Conversation(
        user_id=principal.user_id,
        anonymous_key=principal.key if principal.anonymous else None,
    )
    session.add(conversation)
    await session.flush()
    return conversation


CHARS_PER_TOKEN = 4
# Read at most this many recent messages to fill the history budget.
MAX_HISTORY_ROWS = 200


async def _load_history(session: AsyncSession, conversation: Conversation) -> list[dict[str, str]]:
    """Earlier questions and answers, oldest first, newest kept, within
    CHAT_HISTORY_TOKEN_BUDGET. Tool results are not replayed: this turn's
    tools read the freshest data anyway. A history that would start with an
    answer drops it, so it always starts with a question."""
    budget = settings.chat_history_token_budget * CHARS_PER_TOKEN
    kept: list[ChatMessage] = []
    used = 0
    rows = await session.scalars(
        sa.select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation.id)
        .order_by(ChatMessage.id.desc())
        .limit(MAX_HISTORY_ROWS)
    )
    for message in rows:
        if used + len(message.content) > budget:
            break
        used += len(message.content)
        kept.append(message)
    kept.reverse()
    while kept and kept[0].role != MessageRole.USER:
        kept.pop(0)
    return [{"role": m.role.value, "content": m.content} for m in kept]
