"""Score the labelled snapshots and compute the metrics.

    python -m evals.run --replay                      # no network, no key
    python -m evals.run --replay --gate               # also fail on a regression (CI)
    python -m evals.run --live --budget-usd 2         # re-score for real, recorded
    python -m evals.run --replay --scorer v2          # scorer v2 on the same set

Each labelled movement is scored exactly as ingestion scores it on its
frozen candidates: `relevance.build_scoring_request` and `apply_scoring_result`
for scorer v1, and their `relevance_v2` counterparts for v2, which also see the
snapshot's decomposition, session legs and filings (as candidates). `--replay` answers from `evals/recordings/`; a prompt with no
recording fails the run. `--live` calls the configured provider through the
normal seam, so every call is rate limited and recorded in the spend ledger
(as operation `eval_relevance`), and stops before a call that would take the
run's ledger spend past `--budget-usd`. Results go to
`evals/results/<name>.json`; only `human` and `auto` labels count in the
headline numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.core.config import settings
from app.services.llm import LLMProvider
from app.services.news.base import NewsCandidate
from app.services.news.queries import MovementContext
from app.services.peers import PeerSet
from app.services import relevance_v2
from app.services.relevance import apply_scoring_result, build_scoring_request
from evals import metrics
from evals.metrics import CaseResult, RankedLink
from evals.replay import RecordingLLM
from evals.schema import (
    BASELINE_PATH,
    GOLDEN_PATH,
    HEADLINE_SOURCES,
    RECORDINGS_DIR,
    RESULTS_DIR,
    SNAPSHOTS_DIR,
    Label,
    Snapshot,
    candidate_id,
    load_labels,
    load_snapshots,
)

OPERATION = "eval_relevance"
SCORERS = ("v1", "v2")
# Headline metrics the gate watches, and how far each may fall below the
# baseline before CI fails. Small n makes these noisy; the tolerance is wide.
GATED = ("hit_at_1", "recall_at_5", "tier_accuracy")
DEFAULT_TOLERANCE = 0.05


class BudgetExceeded(RuntimeError):
    pass


def context_for(snapshot: Snapshot) -> MovementContext:
    return MovementContext(
        symbol=snapshot.symbol,
        company_name=snapshot.company_name,
        sector=snapshot.sector,
        industry=snapshot.industry,
        movement_date=snapshot.date,
        daily_return=snapshot.daily_return,
        direction=snapshot.direction,
        window_mode=snapshot.window_mode,
    )


def candidates_for(snapshot: Snapshot) -> list[tuple[str, NewsCandidate]]:
    return [
        (
            c.search_tier,
            NewsCandidate(
                url=c.url, title=c.title, published_at=c.published_at, summary=c.summary,
                content=c.content, source_domain=c.source_domain, provider="snapshot",
            ),
        )
        for c in snapshot.candidates
    ]


def filing_candidates_for(snapshot: Snapshot) -> list[tuple[str, NewsCandidate]]:
    """The snapshot's filings as scorer v2 candidates, described the way
    ingestion describes them."""
    out = []
    for f in snapshot.filings:
        events = [f"Item {item} {event}" for item, event in zip(f.items, f.events)] if len(f.items) == len(f.events) else f.events
        description = f.form + (f": {'; '.join(events)}" if events else "")
        out.append(
            (
                "filing",
                NewsCandidate(
                    url=f.url or f"https://www.sec.gov/edgar/{f.accession}", title=f"SEC filing {description}",
                    published_at=f.accepted_at, summary=f"{description}. Accession {f.accession}.",
                    source_domain="sec.gov", provider="snapshot", source_type="filing", accession=f.accession,
                ),
            )
        )
    return out


def facts_for(snapshot: Snapshot) -> relevance_v2.MovementFacts:
    d, legs = snapshot.decomposition or {}, snapshot.legs or {}
    return relevance_v2.MovementFacts(
        market=d.get("market"), sector=d.get("sector"), company=d.get("abnormal"),
        gap=legs.get("gap"), intraday=legs.get("intraday"),
    )


async def score_case(snapshot: Snapshot, label: Label, llm: LLMProvider, *, scorer: str = "v1") -> CaseResult:
    peers = PeerSet.model_validate(snapshot.peers or {"peers": [], "industry_themes": []})
    if scorer == "v2":
        request = relevance_v2.build_scoring_request_v2(
            context_for(snapshot), candidates_for(snapshot) + filing_candidates_for(snapshot), peers,
            facts=facts_for(snapshot),
        )
    else:
        request = build_scoring_request(context_for(snapshot), candidates_for(snapshot), peers)
    timing = {c.id: c.timing for c in snapshot.candidates}
    timing.update({f.accession: f.timing for f in snapshot.filings})
    if request is None:
        return CaseResult(label=label, ranked=[], notes=["no candidates"])
    started = time.perf_counter()
    report = await llm.parse_structured(
        system=request.system, user=request.user, output_model=request.output_model,
        operation=OPERATION, max_tokens=request.max_tokens,
    )
    latency = time.perf_counter() - started
    notes: list[str] = []
    if scorer == "v2":
        outcome = relevance_v2.apply_scoring_result_v2(request, report, scored_by=llm.model)
        scored = outcome.scored
        if outcome.abstained and outcome.abstain_reason:
            notes.append(f"abstained: {outcome.abstain_reason}")
    else:
        scored = apply_scoring_result(request, report, scored_by=llm.model)
    ranked = []
    for item in scored:
        accession = item.candidate.accession
        key = accession or candidate_id(item.candidate.url)
        ranked.append(
            RankedLink(
                candidate_id=key, url=item.candidate.url, tier=item.tier.value, score=item.score,
                timing=timing.get(key), accession=accession,
            )
        )
    return CaseResult(label=label, ranked=ranked, latency_s=latency, notes=notes)


async def _ledger_spend_since(start: datetime, operation: str = OPERATION) -> Decimal:
    import sqlalchemy as sa

    from app.db.session import SessionLocal
    from app.models.usage import UsageEvent

    async with SessionLocal() as session:
        total = await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(UsageEvent.cost_usd), 0)).where(
                UsageEvent.operation == operation, UsageEvent.created_at >= start
            )
        )
    return Decimal(str(total or 0))


async def evaluate(
    labels: list[Label],
    snapshots: dict[str, Snapshot],
    llm: LLMProvider,
    *,
    budget_usd: Decimal | None = None,
    scorer: str = "v1",
) -> tuple[list[CaseResult], list[str]]:
    cases, skipped = [], []
    started = datetime.now(timezone.utc)
    for label in labels:
        snapshot = snapshots.get(label.key)
        if snapshot is None:
            skipped.append(f"{label.key}: no snapshot")
            continue
        if budget_usd is not None:
            spent = await _ledger_spend_since(started)
            if spent >= budget_usd:
                raise BudgetExceeded(f"spent {spent} of {budget_usd} USD; stopping before {label.key}")
        cases.append(await score_case(snapshot, label, llm, scorer=scorer))
    return cases, skipped


def gate(summary: dict, baseline: dict, tolerance: float = DEFAULT_TOLERANCE) -> list[str]:
    """Regressions beyond `tolerance`, or [] if none (or nothing to compare)."""
    problems = []
    for name in GATED:
        now, then = summary.get(name), (baseline.get("metrics") or {}).get(name)
        if now is None or then is None:
            continue
        if now < then - baseline.get("tolerance", tolerance):
            problems.append(f"{name} fell from {then:.3f} to {now:.3f}")
    if summary.get("timing_violations", 0) > (baseline.get("metrics") or {}).get("timing_violations", 0):
        problems.append("more timing violations than the baseline")
    return problems


def build_llm(args: argparse.Namespace) -> RecordingLLM:
    if args.live:
        from app.services.llm import get_llm_client

        inner = get_llm_client()
        return RecordingLLM(args.recordings, mode="record", model=inner.model, inner=inner)
    return RecordingLLM(args.recordings, mode="replay", model=args.model or settings.llm_model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--budget-usd", type=Decimal, help="required with --live")
    parser.add_argument("--gate", action="store_true", help="exit 1 on a regression against evals/baseline.json")
    parser.add_argument("--scorer", choices=SCORERS, default=settings.relevance_scorer_version)
    parser.add_argument("--name", help="results file name (default scorer-<version>)")
    parser.add_argument("--model", help="model whose recordings to replay (default LLM_MODEL)")
    parser.add_argument("--labels", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--snapshots", type=Path, default=SNAPSHOTS_DIR)
    parser.add_argument("--recordings", type=Path, default=RECORDINGS_DIR)
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)
    if args.live and args.budget_usd is None:
        parser.error("--live needs --budget-usd")
    args.name = args.name or f"scorer-{args.scorer}"

    labels = load_labels(args.labels)
    headline = [label for label in labels if label.label_source in HEADLINE_SOURCES]
    if not headline:
        print(f"0 human or auto labels in {args.labels} ({len(labels)} in all); nothing to score.")
        return 0

    llm = build_llm(args)
    cases, skipped = asyncio.run(evaluate(headline, load_snapshots(args.snapshots), llm, budget_usd=args.budget_usd, scorer=args.scorer))
    summary = metrics.summarise(cases, min_score=settings.relevance_min_score)
    document = {
        "name": args.name,
        "scorer": args.scorer,
        "model": llm.model,
        "mode": "live" if args.live else "replay",
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": " ".join(["python -m evals.run", *(argv if argv is not None else sys.argv[1:])]),
        "labels": {source: sum(label.label_source == source for label in labels) for source in ("human", "auto", "proposed")},
        "skipped": skipped,
        "metrics": summary,
        "cases": [
            {"key": c.label.key, "hit_at_1": c.hit_at_1, "recall_at_5": c.recall_at_5, "abstained": c.abstained,
             "ranked": [asdict(link) for link in c.ranked], "latency_s": round(c.latency_s, 3)}
            for c in cases
        ],
    }
    args.results.mkdir(parents=True, exist_ok=True)
    out = args.results / f"{args.name}.json"
    out.write_text(json.dumps(document, indent=2, default=str) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("calibration", "tier_confusion")}, indent=2, default=str))
    print(f"wrote {out}")

    if args.gate:
        if not BASELINE_PATH.is_file():
            print("No evals/baseline.json yet; nothing to gate against.")
            return 0
        problems = gate(summary, json.loads(BASELINE_PATH.read_text()))
        if problems:
            print("REGRESSION: " + "; ".join(problems))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
