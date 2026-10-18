"""The chat evaluation's deterministic checks, and its runner on fixture data."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.schemas.chat import ArticleSource, ChatResponse, ChatSources, MovementSource
from evals import chat_eval
from tests.conftest import StubLLM
from tests.test_finance_api import ready  # noqa: F401  (fixture)


def test_uncited_numeric_claims():
    answer = "NVDA fell 6.2% on 2026-06-05 [M1]. It was the biggest drop of the year at 6.2%. Nothing else.\nShares cost $120."
    assert chat_eval.uncited_numeric_claims(answer) == ["It was the biggest drop of the year at 6.2%.", "Shares cost $120."]


def test_mismatched_numbers_compare_to_the_cited_movements():
    returns = {"M1": [-6.21, -4.8], "M2": [3.0]}
    assert chat_eval.mismatched_numbers("It fell 6.2% [M1], mostly company news (4.8%).", returns) == []
    problems = chat_eval.mismatched_numbers("It fell 7.5% [M1]. It rose 3% [M2].", returns)
    assert len(problems) == 1 and problems[0].startswith("7.5%")
    assert chat_eval.mismatched_numbers("A 9% move [A3].", returns) == []  # cites no movement: not checked here


def response(answer: str, movements=(), articles=(), removed: int = 0, grounded: bool = True) -> ChatResponse:
    return ChatResponse(conversation_id="c", ticker=None, answer=answer, grounded=grounded, citations_removed=removed,
                        sources=ChatSources(movements=list(movements), articles=list(articles)))


def test_scoring_precision_recall_and_no_data():
    q = chat_eval.Question(id="q", question="?", symbols=["NVDA"], must_movements=[("NVDA", "2026-01-09")],
                           must_articles=["https://a/1"], start=date(2026, 1, 1), end=None)
    on = MovementSource(ref="M1", movement_id=1, symbol="NVDA", date=date(2026, 1, 9), daily_return_pct=-19.88, direction="down")
    off_period = MovementSource(ref="M2", movement_id=2, symbol="NVDA", date=date(2025, 10, 29), daily_return_pct=-16.2, direction="down")
    art = ArticleSource(ref="A5", article_id=5, title="t", url="https://a/1", source=None, published_at=None)
    facts = {"returns": {"M1": [-19.88], "M2": [-16.22]}, "article_links": {"A5": [("NVDA", "2026-01-09")]}}
    case = chat_eval.score(q, response("Fell 19.88% [M1] [A5]. Before, 16.22% [M2].", [on, off_period], [art], removed=1), 0.1, facts)
    assert (case.cited, case.removed, case.relevant) == (3, 1, 2)
    assert case.precision == 0.5 and (case.required, case.required_found) == (2, 2)
    assert case.uncited_numeric_claims == 0 and case.mismatched_numbers == 0 and case.passed_no_data is None

    none = chat_eval.Question(id="n", question="IBM?", symbols=[], must_movements=[], must_articles=[])
    assert chat_eval.score(none, response("No data for IBM.", grounded=False), 0.1, {"returns": {}, "article_links": {}}).passed_no_data


def test_the_question_set_is_well_formed():
    questions = chat_eval.load_questions()
    assert 15 <= len(questions) <= 20
    assert len({q.id for q in questions}) == len(questions)
    assert any(len(q.symbols) > 1 for q in questions) and any(not q.symbols for q in questions)
    for q in questions:
        assert all(symbol in q.symbols for symbol, _ in q.must_movements)


async def test_the_runner_scores_fallback_and_model_answers(session_factory, ready, tmp_path: Path):  # noqa: F811
    _, company_day, _ = ready
    path = tmp_path / "q.jsonl"
    path.write_text(
        '{"id": "a", "question": "Why did SYNTH move so much?", "symbols": ["SYNTH"], '
        f'"must_cite": {{"movements": [["SYNTH", "{company_day.isoformat()}"]], "articles": ["https://n.test/current"]}}}}\n'
        '{"id": "b", "question": "Why did IBM fall?", "symbols": [], "must_cite": {"movements": [], "articles": []}}\n'
    )
    questions = chat_eval.load_questions(path)
    fallback = chat_eval.summarise(await chat_eval.evaluate(questions, chat_eval._NoModel(), session_factory))
    assert fallback["fallback_answers"] == 2 and fallback["citation_precision"] == 1.0
    assert fallback["required_recall"] == 1.0 and fallback["no_data_questions_passed"] == "1/1"
    assert fallback["uncited_numeric_claims"] == 0 and fallback["mismatched_numbers"] == 0

    model = chat_eval.summarise(await chat_eval.evaluate(questions, StubLLM(), session_factory))
    assert model["fallback_answers"] == 0 and model["citation_precision"] == 1.0 and model["mean_tool_calls"] == 0.5


async def test_tool_turns_record_and_replay(tmp_path: Path, session_factory, ready):  # noqa: F811
    from app.models.identity import Plan
    from app.schemas.chat import ChatRequest
    from app.services import chat
    from app.services.auth import Principal
    from evals.replay import RecordingLLM, ReplayMissing

    me = Principal(plan=Plan.PRO, client_ip="9.9.9.9")
    request = ChatRequest(question="Why did SYNTH move so much?")
    async with session_factory() as session:
        live = await chat.answer_question(session, request, RecordingLLM(tmp_path, mode="record", model="m", inner=StubLLM()), me)
    replayer = RecordingLLM(tmp_path, mode="replay", model="m")
    async with session_factory() as session:
        replayed = await chat.answer_question(session, request, replayer, me)
    assert replayer.replayed == 2 and replayed.answer == live.answer and replayed.grounded
    async with session_factory() as session:
        import pytest

        with pytest.raises(ReplayMissing):
            await chat.answer_question(session, ChatRequest(question="Something new about SYNTH?"),
                                       RecordingLLM(tmp_path, mode="replay", model="m"), me)
