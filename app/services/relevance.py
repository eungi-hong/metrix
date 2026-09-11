"""LLM relevance scoring: does this article actually explain this movement?

This is the step that separates a real answer from a plausible-looking one.
A date-windowed search returns everything published near the move, and most of
it is noise -- a routine analyst note, an unrelated sector story, a listicle
that mentions the ticker. Keyword overlap and recency do not establish
explanation.

So every candidate is put to a single model call *per movement*, together with
the movement's size and direction, and asked three questions: is this article
about something that would move this stock, in this direction, at this time?
The model returns a score, a tier, and a one-line rationale that is stored on
the link and shown in the API response.

One call per movement (not per article, not per tier) is deliberate: the model
sees all candidates side by side, which lets it rank them against each other
and reassign the tier when a "macro" search turned up a company-specific story.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import RelevanceTier
from app.models.news import url_fingerprint
from app.services.llm import LLMProvider
from app.services.news.base import NewsCandidate
from app.services.news.queries import MovementContext
from app.services.peers import PeerSet

logger = get_logger(__name__)

IRRELEVANT = "irrelevant"

_SYSTEM = """\
You are a financial news analyst. Given one day on which a stock made an \
unusually large move, and a set of articles published around that day, you \
decide which articles plausibly explain the move.

Judge each article on three things:
1. CAUSALITY -- does it report something that would move this company's share \
price, or is it merely about the company? Routine coverage, listicles, "stocks \
to watch" roundups and after-the-fact recaps that cite the price move as their \
subject explain nothing.
2. DIRECTION -- is the news consistent with the SIGN of the move? A strong \
earnings beat does not explain a large decline unless the article names a \
reason it would (weak guidance, a missed segment).
3. TIMING -- news that broke before or during the session can cause the move. \
Each article's publication date is given relative to the movement, where T+0 \
is the day of the move. News published after the close (T+0 or T+1) can still \
be the explanation if it reports an event from that day. Anything published \
more than one day after the move CANNOT have caused it: score it irrelevant \
regardless of how well its subject matches.

Assign each article a tier describing WHAT THE ARTICLE IS, regardless of which \
search found it:
- "easy"   -- about this company specifically: earnings, guidance, launches, \
lawsuits, filings, management changes, analyst actions on this name.
- "medium" -- about a competitor, supplier, customer, or the industry: a peer's \
results, sector pricing, supply and demand, market-share shifts.
- "hard"   -- macro, political, or regulatory: central bank decisions, \
inflation and jobs data, tariffs, legislation, geopolitics, broad market moves. \
The company need not be mentioned at all.
- "irrelevant" -- does not help explain this move.

Score from 0.0 to 1.0:
  0.90-1.00  the article reports the event that clearly caused the move
  0.60-0.89  strong, plausible explanation consistent with size and direction
  0.35-0.59  contributing factor or partial explanation
  0.00-0.34  irrelevant -- use this freely; most articles near a date are noise

Be strict. A large fraction of any candidate set should be irrelevant. Do not \
inflate scores to produce an answer. Return exactly one assessment per article \
index provided, and never invent an index."""


class ArticleAssessment(BaseModel):
    index: int = Field(description="The article's index from the input list.")
    tier: Literal["easy", "medium", "hard", "irrelevant"]
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(
        max_length=300,
        description="One sentence naming the specific fact that does or does not "
        "connect this article to the move.",
    )


class RelevanceReport(BaseModel):
    assessments: list[ArticleAssessment]


@dataclass(slots=True)
class ScoredCandidate:
    """A candidate that survived scoring, with the verdict attached."""

    candidate: NewsCandidate
    tier: RelevanceTier
    score: float
    rationale: str
    search_tier: str
    scored_by: str


def deduplicate(
    candidates: list[tuple[str, NewsCandidate]],
) -> list[tuple[str, NewsCandidate]]:
    """Collapse the same article arriving from several tier searches.

    The first search to surface an article wins its `search_tier`, which keeps
    the tier ordering (easy, medium, hard) meaningful as a tiebreak.
    """
    seen: set[str] = set()
    unique: list[tuple[str, NewsCandidate]] = []
    for search_tier, candidate in candidates:
        fingerprint = url_fingerprint(candidate.url)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append((search_tier, candidate))
    return unique


async def score_candidates(
    context: MovementContext,
    candidates: list[tuple[str, NewsCandidate]],
    peers: PeerSet,
    llm: LLMProvider,
    min_score: float | None = None,
) -> list[ScoredCandidate]:
    """Score every candidate against one movement; return those worth linking.

    Raises `LLMError` if the model call fails -- the caller decides whether a
    movement without news is acceptable (it is) or fatal (it is not).
    """
    unique = deduplicate(candidates)
    if not unique:
        return []

    threshold = settings.relevance_min_score if min_score is None else min_score
    report = await llm.parse_structured(
        system=_SYSTEM,
        user=_build_prompt(context, unique, peers),
        output_model=RelevanceReport,
        max_tokens=4096,
    )

    by_index = {i: pair for i, pair in enumerate(unique)}
    scored: list[ScoredCandidate] = []
    for assessment in report.assessments:
        pair = by_index.get(assessment.index)
        if pair is None:
            logger.warning(
                "relevance_index_out_of_range",
                symbol=context.symbol,
                index=assessment.index,
            )
            continue
        if assessment.tier == IRRELEVANT or assessment.score < threshold:
            continue
        search_tier, candidate = pair
        scored.append(
            ScoredCandidate(
                candidate=candidate,
                tier=RelevanceTier(assessment.tier),
                score=assessment.score,
                rationale=assessment.rationale.strip(),
                search_tier=search_tier,
                scored_by=llm.model,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    logger.info(
        "relevance_scored",
        symbol=context.symbol,
        date=str(context.movement_date),
        candidates=len(unique),
        linked=len(scored),
        tiers=sorted({s.tier.value for s in scored}),
    )
    return scored


def _build_prompt(
    context: MovementContext,
    candidates: list[tuple[str, NewsCandidate]],
    peers: PeerSet,
) -> str:
    peer_line = ", ".join(f"{p.name} ({p.relationship})" for p in peers.peers) or "unknown"
    themes = ", ".join(peers.industry_themes) or "unknown"

    lines = [
        "MOVEMENT",
        f"  Company: {context.display_name} ({context.symbol})",
        f"  Sector / industry: {context.sector or 'unknown'} / {context.industry or 'unknown'}",
        f"  Known peers: {peer_line}",
        f"  Industry themes: {themes}",
        f"  Date: {context.movement_date.isoformat()}",
        f"  Move: {context.pct} ({context.direction})",
        "",
        "ARTICLES",
    ]
    for index, (search_tier, candidate) in enumerate(candidates):
        lines.extend(
            [
                f"[{index}] title: {candidate.title or '(untitled)'}",
                f"     source: {candidate.source_domain or 'unknown'}"
                f" | published: {_relative_date(candidate, context)}"
                f" | found_by: {search_tier} search",
                f"     text: {candidate.snippet()}",
                "",
            ]
        )
    lines.append(
        f"Assess all {len(candidates)} articles. Return one entry per index, "
        "including the ones you judge irrelevant."
    )
    return "\n".join(lines)


def _relative_date(candidate: NewsCandidate, context: MovementContext) -> str:
    """Publication date expressed relative to the movement, e.g. `2024-05-02 (T-1)`.

    The model reasons about timing far more reliably when the offset is
    computed for it than when it has to subtract two dates itself.
    """
    if candidate.published_at is None:
        return "unknown"
    published = candidate.published_at.date()
    offset = (published - context.movement_date).days
    return f"{published.isoformat()} (T{offset:+d})"
