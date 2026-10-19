"""Relevance metrics, with bootstrap confidence intervals.

Definitions (docs/EVALUATION.md, and the brief's Appendix C):

* Hit@1: the top-ranked link is accepted evidence.
* Recall@5: any accepted item is in the top five.
* Tier accuracy: among movements whose ranking contains accepted evidence,
  the share whose top accepted link has the labelled tier; with a confusion
  matrix of labelled against predicted tier.
* Abstention: the scorer abstains when it links nothing at or above
  RELEVANCE_MIN_SCORE. Precision and recall against "no clear driver" labels.
* Timing violations: links in the `later` bucket scored at or above
  RELEVANCE_MIN_SCORE. Target zero. Counted over every evaluated movement.
* Calibration: the accepted share of links per score bucket.
* Intervals: 1,000 bootstrap resamples over movements (fixed seed), the 2.5th
  and 97.5th percentiles. With a few dozen labels the intervals are wide, and
  saying so is the point.

Hit@1, recall@5 and tier accuracy are over movements that have a driver;
abstention is over all. Pure: case results in, numbers out.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean

from evals.schema import Label

TOP_K = 5
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261018
CI_LOW, CI_HIGH = 0.025, 0.975
CALIBRATION_BUCKETS: tuple[tuple[float, float], ...] = ((0.35, 0.6), (0.6, 0.9), (0.9, 1.0001))


@dataclass(frozen=True, slots=True)
class RankedLink:
    candidate_id: str
    url: str
    tier: str
    score: float
    timing: str | None = None
    accession: str | None = None


@dataclass(slots=True)
class CaseResult:
    label: Label
    ranked: list[RankedLink]
    cost_usd: float = 0.0
    latency_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def accepted(self, link: RankedLink) -> bool:
        return self.label.accepts(candidate_id=link.candidate_id, url=link.url, accession=link.accession)

    @property
    def abstained(self) -> bool:
        return not self.ranked

    @property
    def hit_at_1(self) -> bool:
        return bool(self.ranked) and self.accepted(self.ranked[0])

    @property
    def recall_at_5(self) -> bool:
        return any(self.accepted(link) for link in self.ranked[:TOP_K])

    @property
    def top_accepted(self) -> RankedLink | None:
        return next((link for link in self.ranked if self.accepted(link)), None)


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def hit_at_1(cases: list[CaseResult]) -> float | None:
    return _rate([c.hit_at_1 for c in cases if not c.label.no_clear_driver])


def recall_at_5(cases: list[CaseResult]) -> float | None:
    return _rate([c.recall_at_5 for c in cases if not c.label.no_clear_driver])


def tier_accuracy(cases: list[CaseResult]) -> float | None:
    tops = [(c.label.tier, c.top_accepted) for c in cases if not c.label.no_clear_driver]
    return _rate([top.tier == tier for tier, top in tops if top is not None])


def tier_confusion(cases: list[CaseResult]) -> dict[str, dict[str, int]]:
    """labelled tier -> predicted tier of the top accepted link -> count."""
    matrix: dict[str, Counter[str]] = {}
    for c in cases:
        top = c.top_accepted
        if c.label.no_clear_driver or top is None:
            continue
        matrix.setdefault(c.label.tier, Counter())[top.tier] += 1
    return {tier: dict(counts) for tier, counts in sorted(matrix.items())}


def abstention(cases: list[CaseResult]) -> tuple[float | None, float | None]:
    """(precision, recall) of abstaining, against "no clear driver" labels."""
    abstained = [c for c in cases if c.abstained]
    driverless = [c for c in cases if c.label.no_clear_driver]
    precision = _rate([c.label.no_clear_driver for c in abstained])
    recall = _rate([c.abstained for c in driverless])
    return precision, recall


def timing_violations(cases: list[CaseResult], min_score: float) -> int:
    return sum(1 for c in cases for link in c.ranked if link.timing == "later" and link.score >= min_score)


def calibration(cases: list[CaseResult]) -> list[dict[str, float | int | None]]:
    out = []
    for low, high in CALIBRATION_BUCKETS:
        links = [(c, link) for c in cases for link in c.ranked if low <= link.score < high]
        out.append(
            {
                "low": low,
                "high": min(high, 1.0),
                "links": len(links),
                "accepted_share": _rate([c.accepted(link) for c, link in links]),
            }
        )
    return out


HEADLINE = {
    "hit_at_1": hit_at_1,
    "recall_at_5": recall_at_5,
    "tier_accuracy": tier_accuracy,
    "abstention_precision": lambda cs: abstention(cs)[0],
    "abstention_recall": lambda cs: abstention(cs)[1],
}


def bootstrap(cases: list[CaseResult], resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> dict[str, list[float] | None]:
    """Percentile intervals for each headline metric."""
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {name: [] for name in HEADLINE}
    for _ in range(resamples if cases else 0):
        draw = [cases[rng.randrange(len(cases))] for _ in cases]
        for name, metric in HEADLINE.items():
            value = metric(draw)
            if value is not None:
                samples[name].append(value)
    intervals: dict[str, list[float] | None] = {}
    for name, values in samples.items():
        if not values:
            intervals[name] = None
            continue
        values.sort()
        intervals[name] = [values[int(CI_LOW * (len(values) - 1))], values[int(CI_HIGH * (len(values) - 1))]]
    return intervals


def summarise(cases: list[CaseResult], *, min_score: float) -> dict:
    """Every metric for one set of cases."""
    precision, recall = abstention(cases)
    return {
        "movements": len(cases),
        "with_driver": sum(not c.label.no_clear_driver for c in cases),
        "hit_at_1": hit_at_1(cases),
        "recall_at_5": recall_at_5(cases),
        "tier_accuracy": tier_accuracy(cases),
        "tier_confusion": tier_confusion(cases),
        "abstention_precision": precision,
        "abstention_recall": recall,
        "timing_violations": timing_violations(cases, min_score),
        "calibration": calibration(cases),
        "mean_cost_usd": mean(c.cost_usd for c in cases) if cases else None,
        "mean_latency_s": mean(c.latency_s for c in cases) if cases else None,
        "intervals_95": bootstrap(cases),
    }
