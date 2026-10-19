"""Evaluate Ask Metrix on a fixed question set with deterministic checks.

    python -m evals.chat_eval --fallback                      # no model: the database fallback
    python -m evals.chat_eval --live --budget-usd 1 --name chat-v2
    python -m evals.chat_eval --replay --name chat-v2         # from evals/chat_recordings/

The questions (`evals/chat_questions.jsonl`) are about the synthetic data
`scripts/bench_read_path.py seed` builds, which is deterministic, and name
what an answer must cite by (symbol, date) and article URL rather than by
row id. Point DATABASE_URL at such a database (its name must contain "bench"
or "test": each answer is saved there as a conversation, as the API would).

The checks are deterministic, no model grades anything:

* citation precision: cited rows on the question's tickers (and in its
  period) over all citations, removed ones included;
* required recall: the must-cite movements and articles that were cited;
* uncited numeric claims: sentences with a percentage or dollar figure and
  no citation;
* mismatched numbers: percentages in a sentence that cites movements but
  matching none of their returns (to the rounding shown);
* for questions the data cannot answer: no citations and not grounded.

Results go to evals/results/<name>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.core.config import settings
from app.models.identity import Plan
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.auth import Principal

ROOT = Path(__file__).resolve().parent
QUESTIONS = ROOT / "chat_questions.jsonl"
RECORDINGS = ROOT / "chat_recordings"
RESULTS = ROOT / "results"
PRINCIPAL = Principal(plan=Plan.INTERNAL, client_ip="127.0.0.1")

_NUMBER_CLAIM = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?\s?%|\$\d[\d,]*(?:\.\d+)?")
_PERCENT = re.compile(r"([-+]?\d+(?:\.\d+)?)\s?%")
_LABEL = re.compile(r"\[([MAF]\d+)\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
TOLERANCE = 0.051  # a figure rounded to one or two decimals


@dataclass(slots=True)
class Question:
    id: str
    question: str
    symbols: list[str]
    must_movements: list[tuple[str, str]]
    must_articles: list[str]
    start: date | None = None
    end: date | None = None
    note: str | None = None


@dataclass(slots=True)
class Case:
    id: str
    answered_by: str
    grounded: bool
    cited: int
    removed: int
    relevant: int
    required: int
    required_found: int
    uncited_numeric_claims: int
    mismatched_numbers: int
    passed_no_data: bool | None
    tool_calls: int
    latency_s: float
    answer: str
    problems: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        total = self.cited + self.removed
        return None if total == 0 else self.relevant / total


def load_questions(path: Path = QUESTIONS) -> list[Question]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        out.append(
            Question(
                id=item["id"], question=item["question"], symbols=item["symbols"],
                must_movements=[tuple(m) for m in item["must_cite"]["movements"]],
                must_articles=list(item["must_cite"]["articles"]),
                start=date.fromisoformat(item["start"]) if item.get("start") else None,
                end=date.fromisoformat(item["end"]) if item.get("end") else None,
                note=item.get("note"),
            )
        )
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def uncited_numeric_claims(answer: str) -> list[str]:
    return [s for s in sentences(answer) if _NUMBER_CLAIM.search(s) and not _LABEL.search(s)]


def mismatched_numbers(answer: str, returns: dict[str, list[float]]) -> list[str]:
    """Percentages in sentences citing movements that match none of the cited
    movements' figures (daily return, abnormal return), compared in absolute
    value to the rounding shown. `returns` maps a movement label to its figures
    in percent."""
    problems = []
    for sentence in sentences(answer):
        cited = [label for label in _LABEL.findall(sentence) if label in returns]
        if not cited:
            continue
        figures = [abs(v) for label in cited for v in returns[label]]
        for match in _PERCENT.finditer(sentence):
            value = abs(float(match.group(1)))
            if not any(abs(value - f) <= TOLERANCE for f in figures):
                problems.append(f"{match.group(0)} in: {sentence[:120]}")
    return problems


def score(question: Question, response: ChatResponse, latency: float, facts: dict[str, Any]) -> Case:
    """`facts`: the cited movements' returns by label, and each cited article's
    linked (symbol, date) pairs, read from the database."""
    sources = response.sources
    relevant = 0
    for m in sources.movements:
        in_period = (question.start is None or m.date >= question.start) and (question.end is None or m.date <= question.end)
        relevant += m.symbol in question.symbols and in_period
    for a in sources.articles:
        relevant += any(symbol in question.symbols for symbol, _ in facts["article_links"].get(a.ref, []))
    for f in sources.filings:
        relevant += f.symbol in question.symbols
    cited_movements = {(m.symbol, m.date.isoformat()) for m in sources.movements}
    cited_urls = {a.url for a in sources.articles}
    found = sum(m in cited_movements for m in question.must_movements) + sum(u in cited_urls for u in question.must_articles)
    cited = len(sources.movements) + len(sources.articles) + len(sources.filings)
    no_data = None if question.symbols else (cited == 0 and not response.grounded)
    uncited = uncited_numeric_claims(response.answer)
    mismatched = mismatched_numbers(response.answer, facts["returns"])
    return Case(
        id=question.id, answered_by=response.answered_by, grounded=response.grounded, cited=cited,
        removed=response.citations_removed, relevant=relevant,
        required=len(question.must_movements) + len(question.must_articles), required_found=found,
        uncited_numeric_claims=len(uncited), mismatched_numbers=len(mismatched), passed_no_data=no_data,
        tool_calls=len(response.tool_calls), latency_s=round(latency, 3), answer=response.answer,
        problems=[f"uncited: {s[:120]}" for s in uncited] + [f"mismatch: {p}" for p in mismatched],
    )


def summarise(cases: list[Case]) -> dict[str, Any]:
    precisions = [c.precision for c in cases if c.precision is not None]
    required = sum(c.required for c in cases)
    no_data = [c.passed_no_data for c in cases if c.passed_no_data is not None]
    return {
        "questions": len(cases),
        "citation_precision": round(sum(precisions) / len(precisions), 3) if precisions else None,
        "required_recall": round(sum(c.required_found for c in cases) / required, 3) if required else None,
        "uncited_numeric_claims": sum(c.uncited_numeric_claims for c in cases),
        "mismatched_numbers": sum(c.mismatched_numbers for c in cases),
        "citations_removed": sum(c.removed for c in cases),
        "no_data_questions_passed": f"{sum(no_data)}/{len(no_data)}",
        "grounded_rate": round(sum(c.grounded for c in cases) / len(cases), 3) if cases else None,
        "fallback_answers": sum(c.answered_by == "fallback" for c in cases),
        "mean_tool_calls": round(sum(c.tool_calls for c in cases) / len(cases), 2) if cases else None,
        "median_latency_s": sorted(c.latency_s for c in cases)[len(cases) // 2] if cases else None,
    }


async def facts_for(session, response: ChatResponse) -> dict[str, Any]:
    from app.models.market import Movement, Ticker
    from app.models.news import MovementNewsLink

    returns = {}
    ids = [m.movement_id for m in response.sources.movements]
    if ids:
        for m in (await session.scalars(sa.select(Movement).where(Movement.id.in_(ids)))).all():
            returns[f"M{m.id}"] = [round(m.daily_return * 100, 2)] + (
                [round(m.abnormal_return * 100, 2)] if m.abnormal_return is not None else []
            )
    links: dict[str, list[tuple[str, str]]] = {}
    article_ids = [a.article_id for a in response.sources.articles]
    if article_ids:
        for article_id, symbol, day in (
            await session.execute(
                sa.select(MovementNewsLink.article_id, Ticker.symbol, Movement.date)
                .join(Movement, Movement.id == MovementNewsLink.movement_id)
                .join(Ticker, Ticker.id == Movement.ticker_id)
                .where(MovementNewsLink.article_id.in_(article_ids))
            )
        ).all():
            links.setdefault(f"A{article_id}", []).append((symbol, day.isoformat()))
    return {"returns": returns, "article_links": links}


async def evaluate(questions: list[Question], llm, session_factory, *, budget_usd: Decimal | None = None) -> list[Case]:
    from app.services import chat

    cases = []
    started = datetime.now(timezone.utc)
    for question in questions:
        if budget_usd is not None:
            from evals.run import _ledger_spend_since

            spent = await _ledger_spend_since(started, operation="chat")
            if spent >= budget_usd:
                raise RuntimeError(f"spent {spent} of {budget_usd} USD; stopping before {question.id}")
        request = ChatRequest(question=question.question, start=question.start, end=question.end)
        async with session_factory() as session:
            began = time.perf_counter()
            response = await chat.answer_question(session, request, llm, PRINCIPAL)
            latency = time.perf_counter() - began
            facts = await facts_for(session, response)
        cases.append(score(question, response, latency, facts))
    return cases


class _NoModel:
    """For --fallback: unavailable, so every answer is the database summary."""

    name = "none"
    model = "none"
    available = False


def _require_throwaway(url: str) -> None:
    from sqlalchemy.engine import make_url

    name = make_url(url).database or ""
    if "bench" not in name and "test" not in name:
        raise SystemExit(f"refusing to write conversations into {name!r}: use a seeded bench or test database")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fallback", action="store_true")
    mode.add_argument("--replay", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--budget-usd", type=Decimal)
    parser.add_argument("--name")
    parser.add_argument("--model", help="model whose recordings to replay (default LLM_MODEL)")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--recordings", type=Path, default=RECORDINGS)
    parser.add_argument("--results", type=Path, default=RESULTS)
    args = parser.parse_args(argv)
    if args.live and args.budget_usd is None:
        parser.error("--live needs --budget-usd")
    _require_throwaway(settings.database_url)

    from app.db.session import SessionLocal
    from evals.replay import RecordingLLM

    if args.fallback:
        llm, name = _NoModel(), args.name or "chat-fallback"
    elif args.live:
        from app.services.llm import get_llm_client

        inner = get_llm_client()
        llm, name = RecordingLLM(args.recordings, mode="record", model=inner.model, inner=inner), args.name or "chat-live"
    else:
        llm = RecordingLLM(args.recordings, mode="replay", model=args.model or settings.llm_model)
        name = args.name or "chat-replay"

    cases = asyncio.run(evaluate(load_questions(args.questions), llm, SessionLocal, budget_usd=args.budget_usd))
    summary = summarise(cases)
    args.results.mkdir(parents=True, exist_ok=True)
    document = {
        "name": name,
        "mode": "fallback" if args.fallback else "live" if args.live else "replay",
        "model": getattr(llm, "model", None),
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": " ".join(["python -m evals.chat_eval", *(argv if argv is not None else sys.argv[1:])]),
        "metrics": summary,
        "cases": [asdict(c) | {"precision": c.precision} for c in cases],
    }
    (args.results / f"{name}.json").write_text(json.dumps(document, indent=2, default=str) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
