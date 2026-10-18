"""Benchmark choice, and the check that keeps the sector map honest.

The default map is PROVISIONAL: its keys are the yfinance sector and industry
strings the v2 brief lists, not yet checked against real responses. Drop real
profiles into tests/fixtures/yfinance_profiles.json (the output of the command
in docs/v2/BASELINE.md's Phase 1 notes) and
`test_every_real_profile_string_has_a_benchmark` checks every one of them,
failing with the list of strings the map does not cover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import PROVISIONAL_INDUSTRY_BENCHMARKS, PROVISIONAL_SECTOR_BENCHMARKS, Settings, settings
from app.services import benchmarks

REAL_PROFILES = Path(__file__).parent / "fixtures" / "yfinance_profiles.json"


def test_industry_overrides_sector():
    choice = benchmarks.choose("Technology", "Semiconductors")
    assert (choice.market, choice.sector, choice.note) == ("SPY", "SMH", None)
    assert benchmarks.choose("Technology", "Software - Infrastructure").sector == "XLK"


def test_an_unmapped_sector_is_market_only_and_says_so():
    choice = benchmarks.choose("Conglomerates", None)
    assert choice.sector is None
    assert "Conglomerates" in (choice.note or "")
    assert benchmarks.choose(None, None).note is not None


def test_every_benchmark_is_refreshed_and_recognised():
    symbols = benchmarks.all_symbols()
    assert "SPY" in symbols
    assert set(PROVISIONAL_SECTOR_BENCHMARKS.values()) <= set(symbols)
    assert set(PROVISIONAL_INDUSTRY_BENCHMARKS.values()) <= set(symbols)
    assert benchmarks.is_benchmark("xlk") and not benchmarks.is_benchmark("NVDA")


def test_the_map_is_configuration(monkeypatch):
    monkeypatch.setenv("BENCHMARK_SECTOR_MAP_JSON", '{"Technology": " rspt "}')
    custom = Settings()
    assert custom.benchmark_sector_map_json == {"Technology": "RSPT"}
    assert benchmarks.choose("Technology", None, custom).sector == "RSPT"
    assert benchmarks.choose("Energy", None, custom).sector is None  # replaced whole

    monkeypatch.setenv("BENCHMARK_SECTOR_MAP_JSON", "[1, 2]")
    with pytest.raises(ValueError, match="BENCHMARK_SECTOR_MAP_JSON"):
        Settings()


def test_unmapped_reports_symbols_whose_sector_would_get_no_benchmark():
    profiles = {
        "NVDA": {"sector": "Technology", "industry": "Semiconductors"},
        "ODD": {"sector": "Information Technology", "industry": None},
        "NONE": {"sector": None, "industry": None},
    }
    assert set(benchmarks.unmapped(profiles, settings)) == {"ODD"}


def test_every_real_profile_string_has_a_benchmark():
    if not REAL_PROFILES.is_file():
        pytest.skip(
            "PENDING: tests/fixtures/yfinance_profiles.json (real yfinance profiles) has "
            "not been added yet; the sector map is provisional until it is."
        )
    profiles = json.loads(REAL_PROFILES.read_text())
    problems = benchmarks.unmapped(profiles, settings)
    assert not problems, (
        "Real yfinance sector strings with no benchmark in BENCHMARK_SECTOR_MAP_JSON: "
        + ", ".join(f"{symbol}: {why}" for symbol, why in sorted(problems.items()))
    )
