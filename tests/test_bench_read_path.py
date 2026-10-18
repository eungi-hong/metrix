"""Tests for scripts/bench_read_path.py, the synthetic-local read-path benchmark.

Only the pure parts: the synthetic price series it seeds, the percentile
summary it reports, and the checks that stop it timing the wrong thing. The
numbers in docs/v2/BASELINE.md come from these functions, so they are held to
known answers here rather than trusted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bench_read_path as bench  # noqa: E402


def test_synthetic_history_is_deterministic_per_symbol():
    first = bench.synthetic_history("NVDA")
    again = bench.synthetic_history("NVDA")
    other = bench.synthetic_history("AAPL")

    assert first.bars == again.bars
    assert [b.close for b in first.bars] != [b.close for b in other.bars]


def test_synthetic_history_is_weekdays_only_and_internally_consistent():
    history = bench.synthetic_history("TSLA")

    assert history.bars, "a year of bars"
    assert all(bar.date.weekday() < 5 for bar in history.bars)
    assert history.bars[-1].date <= bench.DEFAULT_END
    for bar in history.bars:
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)
        assert bar.adj_close == bar.close  # no corporate actions in synthetic data
        assert bar.volume > 0
    assert history.profile.company_name == "TSLA (synthetic)"


def test_summary_percentiles_on_a_known_series():
    # 1..100 ms: the inclusive quantiles of this series are exact.
    latencies = [ms / 1000 for ms in range(1, 101)]
    summary = bench.summarize("default", 1, latencies, errors=0, wall_s=2.0, response_bytes=10)

    assert summary.requests == 100
    assert summary.p50_ms == 50.5
    assert summary.p95_ms == 95.05
    assert summary.p99_ms == 99.01
    assert summary.max_ms == 100.0
    assert summary.mean_ms == 50.5
    assert summary.throughput_rps == 50.0


def test_summary_counts_errors_in_requests_and_throughput_but_not_latency():
    summary = bench.summarize("default", 2, [0.010, 0.020], errors=2, wall_s=1.0, response_bytes=1)

    assert summary.requests == 4
    assert summary.errors == 2
    assert summary.max_ms == 20.0
    assert summary.throughput_rps == 4.0


def test_summary_refuses_an_empty_series():
    with pytest.raises(ValueError):
        bench.summarize("default", 1, [], errors=5, wall_s=1.0, response_bytes=0)


def warm_payload(**overrides):
    payload = {"status": "ready", "job_id": None, "warnings": [], "movements": [{"id": 1}]}
    return payload | overrides


def test_a_warm_ready_response_passes():
    assert bench.check_warm(warm_payload()) == []


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"status": "refreshing"}, "status"),
        ({"job_id": 17}, "job"),
        ({"warnings": ["Showing stored data; nothing new was fetched"]}, "warnings"),
        ({"movements": []}, "no movements"),
    ],
)
def test_anything_but_a_warm_read_is_refused(overrides, problem):
    problems = bench.check_warm(warm_payload(**overrides))

    assert any(problem in p for p in problems)


def test_payload_description_counts_what_was_returned():
    body = json.dumps(
        {
            "pagination": {"total": 24, "returned": 2},
            "movements": [{"news": [{}, {}, {}]}, {"news": []}],
            "prices": [{}] * 251,
        }
    ).encode()

    payload = bench.describe_payload(
        "frontend", {"limit": "50"}, 200, body, {"x-ratelimit-limit": "1000000"}
    )

    assert payload.movements_total == 24
    assert payload.movements_returned == 2
    assert payload.links_returned == 3
    assert payload.price_bars_returned == 251
    assert payload.body_bytes == len(body)
    assert payload.ratelimit_limit == "1000000"


def test_the_runs_report_gives_the_median_and_range_per_series():
    def run(p50: float, p95: float) -> dict:
        row = {"shape": "default", "concurrency": 1, "p50_ms": p50, "p95_ms": p95,
               "p99_ms": p95 + 5, "throughput_rps": 1000 / p50, "response_bytes": 44849}
        return {"provenance": {"label": "synthetic-local"}, "results": [row]}

    table = bench.runs_table([run(40.0, 50.0), run(30.0, 45.0), run(35.0, 47.0)])
    row = table.splitlines()[2]

    assert row.startswith("| default | 1 | 3 | 35 (30 to 40) | 47 (45 to 50) | 52 (50 to 55) |")
    assert row.endswith("| 44849 |")


def test_one_run_is_reported_without_a_range():
    assert bench._median_range([12.5]) == "12.5"


def test_seeding_refuses_a_database_not_named_for_benchmarks():
    with pytest.raises(SystemExit):
        bench._require_throwaway_database("postgresql+asyncpg://metrix:metrix@db:5432/metrix")
    bench._require_throwaway_database("postgresql+asyncpg://m:m@127.0.0.1:5433/metrix_bench")
