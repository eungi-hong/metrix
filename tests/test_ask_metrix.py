"""Ask Metrix 2.0: the bounded tool loop, citation checks, multi-ticker
resolution, the database fallback, history by token budget, suggestions."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.core.errors import LLMError
from app.models.chat import ChatMessage, Conversation
from app.models.enums import MessageRole
from app.models.market import Movement, Ticker
from app.services import chat, ingestion
from app.services.llm.base import ToolCall, ToolTurn
from tests.conftest import StubLLM, api_app, api_client
from tests.test_finance_api import ready  # noqa: F401  (fixture)


class Scripted(StubLLM):
    """Each model call runs the next step: a function of the transcript that
    returns a ToolTurn. Records the tools offered on each call."""

    def __init__(self, *steps) -> None:
        super().__init__()
        self.steps = list(steps)
        self.offered: list[list[str]] = []

    async def tool_turn(self, *, messages, tools, on_text=None, **kwargs) -> Any:
        self.offered.append([t.name for t in tools])
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        turn = step(messages)
        if on_text is not None and turn.text:
            await on_text(turn.text)
        return turn


def call(name: str, **arguments) -> ToolTurn:
    return ToolTurn(text="", tool_calls=[ToolCall(f"c-{name}", name, arguments)], input_tokens=100, output_tokens=10)


def labels_in(messages, prefix: str) -> list[str]:
    tool_text = "".join(m["content"] for m in messages if m["role"] == "tool")
    return [label for label in re.findall(r'"label":"([MAF]\d+)"', tool_text) if label.startswith(prefix)]


@pytest.fixture
async def asker(session_factory, user_key):
    def make(llm):
        return api_client(api_app(session_factory, llm), user_key[1])

    return make


# -- citations ---------------------------------------------------------------


def test_only_labels_the_tools_returned_survive():
    text = "Fell 6% [M12] on a ban [A9][A404]. Peers too [M12, M999; F3]. See [note] and [M5]."
    cleaned, cited, stripped = chat.check_citations(text, {"M12", "A9", "F3"})
    assert cleaned == "Fell 6% [M12] on a ban [A9]. Peers too [M12][F3]. See [note] and."
    assert cited == ["M12", "A9", "F3"] and stripped == ["A404", "M999", "M5"]


async def test_an_invented_citation_is_stripped_and_only_cited_rows_are_sources(asker, ready):  # noqa: F811
    def answer(messages):
        first, *rest = labels_in(messages, "M")
        assert rest, "the lookup returned several movements"
        return ToolTurn(text=f"It fell on a guidance cut [{first}] [A99999999].")

    llm = Scripted(lambda m: call("get_movements", symbol="SYNTH", limit=5), answer)
    async with asker(llm) as client:
        body = (await client.post("/chat", json={"question": "Why did SYNTH fall?"})).json()
    (cited,) = body["sources"]["movements"]
    assert body["answer"] == f"It fell on a guidance cut [{cited['ref']}]."
    assert body["sources"]["articles"] == []
    assert body["grounded"] is True


async def test_an_answer_that_cites_nothing_is_not_grounded(asker, ready):  # noqa: F811
    llm = Scripted(lambda m: call("get_movements", symbol="SYNTH"), lambda m: ToolTurn(text="It moved a lot."))
    async with asker(llm) as client:
        body = (await client.post("/chat", json={"question": "Why did SYNTH move?"})).json()
    assert body["grounded"] is False and body["sources"] == {"movements": [], "articles": [], "filings": []}
    assert body["answered_by"] == "model" and body["tool_calls"][0]["name"] == "get_movements"


# -- bounds ----------------------------------------------------------------------


async def test_at_most_five_tool_calls_then_an_answer_without_tools(asker, ready, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "chat_max_tool_calls", 5)

    def greedy(messages):
        if len([m for m in messages if m["role"] == "tool"]) >= 6:
            return ToolTurn(text=f"Done [{labels_in(messages, 'M')[0]}].")
        return ToolTurn(text="", tool_calls=[ToolCall(f"c{i}", "get_movements", {"symbol": "SYNTH"}) for i in range(2)])

    llm = Scripted(greedy)
    async with asker(llm) as client:
        body = (await client.post("/chat", json={"question": "SYNTH?"})).json()
    assert len(body["tool_calls"]) == 5
    assert llm.offered[-1] == [] and all(llm.offered[:-1])  # the last call could only answer
    assert body["grounded"] is True


async def test_the_token_budget_ends_tool_use(asker, ready, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "chat_turn_token_budget", 2_000)

    def heavy(messages):
        if not any(m["role"] == "tool" for m in messages):
            return ToolTurn(text="", tool_calls=[ToolCall("c", "get_movements", {"symbol": "SYNTH"})],
                            input_tokens=2_500, output_tokens=10)
        return ToolTurn(text=f"Answer [{labels_in(messages, 'M')[0]}].", input_tokens=2_600, output_tokens=10)

    llm = Scripted(heavy)
    async with asker(llm) as client:
        body = (await client.post("/chat", json={"question": "SYNTH?"})).json()
    assert llm.offered == [[t.name for t in chat.chat_tools.TOOLS], []]
    assert len(body["tool_calls"]) == 1


async def test_an_empty_answer_falls_back(asker, ready):  # noqa: F811
    llm = Scripted(lambda m: ToolTurn(text="   "))
    async with asker(llm) as client:
        body = (await client.post("/chat", json={"question": "Why did SYNTH move?"})).json()
    assert body["answered_by"] == "fallback"


# -- the fallback ----------------------------------------------------------------


class Down(StubLLM):
    async def tool_turn(self, **kwargs):
        raise LLMError("openai", "HTTP 500")


async def test_the_fallback_summarises_the_largest_moves_with_citations(asker, ready):  # noqa: F811
    _, company_day, _ = ready
    async with asker(Down()) as client:
        body = (await client.post("/chat", json={"question": "Why did SYNTH move?"})).json()
    assert body["answered_by"] == "fallback" and body["answer"].startswith(chat.FALLBACK_HEADER)
    assert body["grounded"] is True and body["tool_calls"] == []
    assert company_day.isoformat() in body["answer"]
    assert "https://n.test/current" in body["answer"]  # its strongest evidence
    assert {s["date"] for s in body["sources"]["movements"]} >= {company_day.isoformat()}
    assert body["sources"]["articles"][0]["url"] == "https://n.test/current"


async def test_the_fallback_without_a_ticker_asks_for_one(asker):
    async with asker(Down()) as client:
        body = (await client.post("/chat", json={"question": "What happened?"})).json()
    assert body["answered_by"] == "fallback" and "Name a ticker" in body["answer"] and not body["grounded"]


async def test_an_unconfigured_model_falls_back_without_calling_it(session_factory, user_key, ready):  # noqa: F811
    class Unconfigured(StubLLM):
        @property
        def available(self) -> bool:
            return False

        async def tool_turn(self, **kwargs):  # pragma: no cover
            raise AssertionError("called")

    async with api_client(api_app(session_factory, Unconfigured()), user_key[1]) as client:
        body = (await client.post("/chat", json={"question": "SYNTH?"})).json()
    assert body["answered_by"] == "fallback"


# -- tickers, history, suggestions ------------------------------------------------


async def held(session, symbol: str, name: str) -> Ticker:
    from datetime import datetime, timezone

    ticker = await ingestion.get_or_create_ticker(session, symbol)
    ticker.company_name = name
    ticker.last_ingested_at = datetime.now(timezone.utc)
    return ticker


async def test_several_tickers_are_resolved_in_order_by_symbol_or_name(session_factory):
    async with session_factory() as session:
        await held(session, "NVDA", "NVIDIA Corporation")
        await held(session, "AMD", "Advanced Micro Devices, Inc.")
        await held(session, "AAPL", "Apple Inc.")
        session.add(Ticker(symbol="IT"))  # held but never researched: ignored
        await session.commit()
        found = await chat.infer_tickers(session, "Did AMD and NVDA fall for the same reason in June?")
        assert [t.symbol for t in found] == ["AMD", "NVDA"]
        named = await chat.infer_tickers(session, "Compare nvidia with apple, IT and I mean it")
        assert [t.symbol for t in named] == ["NVDA", "AAPL"]
        assert [t.symbol for t in await chat.infer_tickers(session, "What about $amd?")] == ["AMD"]
        assert await chat.infer_tickers(session, "Is THE market up?") == []


async def test_a_multi_ticker_question_reaches_the_model_with_both(asker, session_factory, ready):  # noqa: F811
    seen = []

    def first(messages):
        seen.append(messages[-1]["content"])
        return ToolTurn(text="Not enough data.")

    async with session_factory() as session:
        await held(session, "PEER", "Peer Industries")
        await session.commit()
    async with asker(Scripted(first)) as client:
        body = (await client.post("/chat", json={"question": "Did SYNTH and PEER move together?"})).json()
    assert seen[0].startswith("TICKERS: SYNTH, PEER") and body["ticker"] == "SYNTH"
    assert "Did SYNTH and PEER move for the same reasons this year?" in body["suggestions"]


async def test_history_is_trimmed_to_its_token_budget_newest_first(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "chat_history_token_budget", 100)  # about 400 characters
    async with session_factory() as session:
        conversation = Conversation(anonymous_key="anon:x")
        session.add(conversation)
        await session.flush()
        for i in range(6):
            session.add(ChatMessage(conversation_id=conversation.id, role=MessageRole.USER, content=f"Question {i}?"))
            session.add(ChatMessage(conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="x" * 150))
        await session.commit()
        history = await chat._load_history(session, conversation)
    assert history[0] == {"role": "user", "content": "Question 4?"}  # starts with a question
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert sum(len(m["content"]) for m in history) <= 400


async def test_suggestions_come_from_the_data(session_factory, ready):  # noqa: F811
    async with session_factory() as session:
        down = await session.scalar(
            sa.select(Movement).where(Movement.direction == "down", Movement.qualifies.is_(True))
            .order_by(Movement.abs_return.desc()).limit(1)
        )
        suggestions = await chat.suggest_follow_ups(session, ["SYNTH"], "anything")
        assert suggestions[0] == f"Why did SYNTH fall {down.abs_return * 100:.1f}% on {down.date.isoformat()}?"
        assert len(suggestions) == 3
        assert suggestions[0] not in await chat.suggest_follow_ups(session, ["SYNTH"], suggestions[0])
        assert await chat.suggest_follow_ups(session, [], "x") == []


async def test_the_stable_prefix_is_identical_across_questions(asker, ready):  # noqa: F811
    llm = StubLLM()
    async with asker(llm) as client:
        await client.post("/chat", json={"question": "Why did SYNTH fall?"})
        await client.post("/chat", json={"question": "And on its best day?", "ticker": "SYNTH"})
    systems = {c["system"] for c in llm.tool_turn_calls}
    assert systems == {chat.SYSTEM}
    assert json.dumps([t.parameters for t in chat.chat_tools.TOOLS])  # serialisable, static
