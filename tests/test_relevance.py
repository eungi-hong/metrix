"""Tests for the scoring split: building the request and applying the result
are pure, so a batch scorer can do them at different times."""

from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import RelevanceTier
from app.services.news.base import NewsCandidate
from app.services.news.queries import MovementContext
from app.services.peers import PeerSet
from app.services.relevance import (
    ArticleAssessment,
    RelevanceReport,
    apply_scoring_result,
    build_scoring_request,
    score_candidates,
)

CONTEXT = MovementContext(
    symbol="TEST", company_name="Test Industries", sector="Technology",
    industry="Widgets", movement_date=date(2026, 7, 2), daily_return=-0.08,
    direction="down",
)


def candidates() -> list[tuple[str, NewsCandidate]]:
    return [
        ("easy", NewsCandidate(url="https://n.test/a", title="Earnings miss")),
        ("hard", NewsCandidate(url="https://www.n.test/a/", title="Same story, other tier")),
        ("hard", NewsCandidate(url="https://n.test/b", title="Fed hikes")),
        ("medium", NewsCandidate(url="https://n.test/c", title="Listicle")),
    ]


def assess(index: int, tier: str, score: float) -> ArticleAssessment:
    return ArticleAssessment(index=index, tier=tier, score=score, rationale=f" r{index} ")


def test_the_request_indexes_deduplicated_candidates():
    request = build_scoring_request(CONTEXT, candidates(), PeerSet())

    assert [c.url for _, c in request.candidates] == [
        "https://n.test/a", "https://n.test/b", "https://n.test/c",
    ]
    assert "[2] title: Listicle" in request.user
    assert "Test Industries (TEST)" in request.user
    assert request.output_model is RelevanceReport


def test_nothing_to_score_builds_no_request():
    assert build_scoring_request(CONTEXT, [], PeerSet()) is None


def test_applying_a_result_filters_and_ranks():
    request = build_scoring_request(CONTEXT, candidates(), PeerSet())
    report = RelevanceReport(
        assessments=[
            assess(0, "easy", 0.6),
            assess(1, "hard", 0.9),
            assess(2, "irrelevant", 0.8),  # irrelevant regardless of score
            assess(7, "easy", 0.99),  # an index the model invented
        ]
    )

    scored = apply_scoring_result(request, report, scored_by="m", min_score=0.5)

    assert [(s.candidate.url, s.tier, s.score) for s in scored] == [
        ("https://n.test/b", RelevanceTier.HARD, 0.9),
        ("https://n.test/a", RelevanceTier.EASY, 0.6),
    ]
    assert scored[0].search_tier == "hard"
    assert scored[0].rationale == "r1"
    assert scored[0].scored_by == "m"


async def test_score_candidates_is_build_then_call_then_apply(stub_llm):
    scored = await score_candidates(CONTEXT, candidates(), PeerSet(), stub_llm)

    (call,) = stub_llm.structured_calls
    request = build_scoring_request(CONTEXT, candidates(), PeerSet())
    assert call["user"] == request.user and call["system"] == request.system
    assert [s.score for s in scored] == pytest.approx([0.91, 0.62])
