"""Scorer v2: the same one call per movement, with what an analyst would ask.

v1 (`app.services.relevance`) shows the model a move's size and direction and
each article's date relative to it in calendar days. v2 adds, still in one
call per movement and with the same build/apply split:

* the decomposition: "about 70% of this move is company-specific, 25%
  sector, 5% market", so a mostly-sector day points at sector news;
* the session legs: "most of the move happened at the open";
* each candidate's session bucket (`app.services.sessions`) instead of a
  calendar offset, and the rule that after-close and later news cannot have
  caused the close-to-close move;
* SEC filings in the window as candidates (`source_type = filing`);
* a source-quality hint per candidate: primary (filings, wire releases,
  investor relations), tier-1 newsroom, or other.

It returns a `role` per kept link (primary, contributing, context) and a
movement-level `abstain` with a reason when nothing explains the move.

One guard is deterministic rather than prompted: a candidate published, by a
known time, after the next session's open (`later`) is never linked, so
timing violations are zero by construction. The model still scores it, and
the score is kept in the recording.

v1 stays selectable (`RELEVANCE_SCORER_VERSION`); v2 becomes the default only
if the evaluation says it is better.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import RelevanceTier
from app.services import sessions
from app.services.news.base import NewsCandidate
from app.services.news.queries import MovementContext
from app.services.peers import PeerSet
from app.services.relevance import IRRELEVANT, ScoredCandidate, deduplicate

logger = get_logger(__name__)

SCORER_VERSION = "v2"
Role = Literal["primary", "contributing", "context"]
SourceQuality = Literal["primary", "tier1", "other"]
SCORING_MAX_TOKENS = 4096
# A leg is "most of the move" when it is at least this share of both legs.
DOMINANT_LEG_SHARE = 0.6

_SYSTEM = """\
You are a financial analyst. A stock made an unusually large move on one \
trading session (T). You are given what the move was made of, when in the \
session it happened, and a set of candidate sources published around it: news \
articles and SEC filings. Decide which candidates explain the move.

Use the decomposition. A move that is mostly market or sector is usually \
explained by market-wide, sector or peer news; a mostly company-specific move \
needs company news. Use the session legs: a move made at the open was caused \
by something before it (overnight or earlier); a move made during the day by \
something intraday.

Timing, in New York Stock Exchange sessions (P is the session before T):
- before_prior_close: published before P's close, so mostly priced in by T. \
Weak evidence unless it is the event itself.
- overnight: after P's close, before T's open. The classic catalyst (after-hours \
earnings, pre-market news). Explains the opening gap.
- intraday: during T. Explains the intraday leg.
- after_close: after T's close. CANNOT have caused T's close-to-close move; \
at most context (a recap that names the cause).
- later: after the next session opened. CANNOT have caused it.
- unknown: no usable time; judge on content, cautiously.

Source quality: primary sources (SEC filings, company wire releases, investor \
relations) report events first-hand; tier-1 newsrooms are reliable secondary \
reporting; other sources are weaker. Prefer the primary source of an event \
when several report it.

For each candidate return its tier (what it is about: easy = this company, \
medium = peers or industry, hard = macro, political or regulatory, irrelevant), \
a score from 0.0 to 1.0 (0.90+ reports the event that clearly caused the move; \
0.60 to 0.89 a strong, consistent explanation; 0.35 to 0.59 a contributing \
factor; below 0.35 irrelevant), a role (primary: the cause; contributing: \
part of it; context: background that does not itself explain it), and one \
sentence naming the fact that connects it to the move or does not.

If no candidate explains the move, set abstain to true and say why in one \
sentence. Be strict: most candidates are noise. Return one assessment per \
index and never invent one."""


class AssessmentV2(BaseModel):
    index: int
    tier: Literal["easy", "medium", "hard", "irrelevant"]
    score: float = Field(ge=0.0, le=1.0)
    role: Literal["primary", "contributing", "context"]
    rationale: str = Field(max_length=300)


class RelevanceReportV2(BaseModel):
    assessments: list[AssessmentV2]
    abstain: bool = Field(description="True when no candidate explains the move.")
    abstain_reason: str | None = Field(default=None, max_length=300)


@dataclass(frozen=True, slots=True)
class MovementFacts:
    """The decomposition and legs, as returns (None where not computed)."""

    market: float | None = None
    sector: float | None = None
    company: float | None = None
    gap: float | None = None
    intraday: float | None = None


@dataclass(frozen=True, slots=True)
class ScoringRequestV2:
    context: MovementContext
    candidates: list[tuple[str, NewsCandidate]]
    timings: list[sessions.ArticleTiming]
    system: str
    user: str
    output_model: type[RelevanceReportV2] = RelevanceReportV2
    max_tokens: int = SCORING_MAX_TOKENS


@dataclass(slots=True)
class ScoredCandidateV2(ScoredCandidate):
    role: str = "context"
    timing: str | None = None


@dataclass(slots=True)
class ScoringOutcomeV2:
    scored: list[ScoredCandidateV2]
    abstained: bool
    abstain_reason: str | None


def source_quality(candidate: NewsCandidate) -> SourceQuality:
    if candidate.source_type == "filing":
        return "primary"
    host = (urlsplit(candidate.url).hostname or "").lower()
    primary = {d.strip().lower() for d in settings.source_primary_domains.split(",") if d.strip()}
    tier1 = {d.strip().lower() for d in settings.source_tier1_domains.split(",") if d.strip()}

    def matches(domains: set[str]) -> bool:
        return any(host == d or host.endswith("." + d) for d in domains)

    if matches(primary) or host.startswith(("ir.", "investor")):
        return "primary"
    if matches(tier1):
        return "tier1"
    return "other"


def describe_facts(facts: MovementFacts | None, total: float) -> list[str]:
    """The decomposition and legs as the sentences the prompt carries."""
    lines: list[str] = []
    if facts and None not in (facts.market, facts.sector, facts.company):
        parts = {"company-specific": abs(facts.company), "sector": abs(facts.sector), "market": abs(facts.market)}
        whole = sum(parts.values())
        if whole > 0:
            shares = ", ".join(f"{100 * v / whole:.0f}% {name}" for name, v in parts.items())
            lines.append(
                f"  Decomposition: about {shares} (market {facts.market:+.2%}, sector {facts.sector:+.2%}, "
                f"company {facts.company:+.2%} of a {total:+.2%} move)"
            )
    if facts and facts.gap is not None and facts.intraday is not None:
        legs = abs(facts.gap) + abs(facts.intraday)
        if legs > 0 and abs(facts.gap) / legs >= DOMINANT_LEG_SHARE:
            where = "most of the move happened at the open"
        elif legs > 0 and abs(facts.intraday) / legs >= DOMINANT_LEG_SHARE:
            where = "most of the move happened during the session"
        else:
            where = "the move was split between the open and the session"
        lines.append(f"  Session legs: {where} (gap {facts.gap:+.2%}, intraday {facts.intraday:+.2%})")
    return lines


def _published(candidate: NewsCandidate) -> str:
    if candidate.published_at is None:
        return "unknown"
    return candidate.published_at.astimezone(sessions.NEW_YORK).strftime("%Y-%m-%d %H:%M New York")


def build_scoring_request_v2(
    context: MovementContext,
    candidates: list[tuple[str, NewsCandidate]],
    peers: PeerSet,
    *,
    facts: MovementFacts | None,
) -> ScoringRequestV2 | None:
    unique = deduplicate(candidates)
    if not unique:
        return None
    frame = sessions.frame_for(context.movement_date)
    timings = [sessions.classify(candidate.published_at, frame) for _, candidate in unique]
    peer_line = ", ".join(f"{p.name} ({p.relationship})" for p in peers.peers) or "unknown"
    lines = [
        "MOVEMENT",
        f"  Company: {context.display_name} ({context.symbol})",
        f"  Sector / industry: {context.sector or 'unknown'} / {context.industry or 'unknown'}",
        f"  Known peers: {peer_line}",
        f"  Session: {context.movement_date.isoformat()}",
        f"  Move: {context.pct} ({context.direction}), close to close",
        *describe_facts(facts, context.daily_return),
        "",
        "CANDIDATES",
    ]
    limit = settings.relevance_snippet_chars
    for index, ((search_tier, candidate), timing) in enumerate(zip(unique, timings)):
        bucket = timing.timing or "unknown"
        if timing.precision == "day":
            bucket += " (date only)"
        lines.extend(
            [
                f"[{index}] title: {candidate.title or '(untitled)'}",
                f"     source_type: {candidate.source_type} | quality: {source_quality(candidate)}"
                f" | source: {candidate.source_domain or 'unknown'} | found_by: {search_tier}",
                f"     published: {_published(candidate)} | timing: {bucket}",
                f"     text: {candidate.snippet(limit)}",
                "",
            ]
        )
    lines.append(f"Assess all {len(unique)} candidates, one entry per index.")
    return ScoringRequestV2(
        context=context, candidates=unique, timings=timings, system=_SYSTEM, user="\n".join(lines)
    )


def apply_scoring_result_v2(
    request: ScoringRequestV2,
    report: RelevanceReportV2,
    *,
    scored_by: str,
    min_score: float | None = None,
) -> ScoringOutcomeV2:
    threshold = settings.relevance_min_score if min_score is None else min_score
    if report.abstain:
        return ScoringOutcomeV2([], True, (report.abstain_reason or "").strip() or None)
    scored: list[ScoredCandidateV2] = []
    for assessment in report.assessments:
        if not 0 <= assessment.index < len(request.candidates):
            logger.warning("relevance_index_out_of_range", symbol=request.context.symbol, index=assessment.index)
            continue
        if assessment.tier == IRRELEVANT or assessment.score < threshold:
            continue
        timing = request.timings[assessment.index]
        if timing.timing == sessions.LATER and timing.precision == sessions.PRECISION_TIME:
            continue  # cannot have caused the move; never linked
        search_tier, candidate = request.candidates[assessment.index]
        scored.append(
            ScoredCandidateV2(
                candidate=candidate,
                tier=RelevanceTier(assessment.tier),
                score=assessment.score,
                rationale=assessment.rationale.strip(),
                search_tier=search_tier,
                scored_by=scored_by,
                role=assessment.role,
                timing=timing.timing,
            )
        )
    scored.sort(key=lambda s: s.score, reverse=True)
    return ScoringOutcomeV2(scored, not scored, None if scored else "nothing scored above the threshold")
