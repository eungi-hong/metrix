"""scripts/measure_detection.py, offline: synthetic histories in place of yfinance.

The script's numbers go into the README and decide whether union becomes the
default, so its counting and its cost arithmetic are pinned here.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import benchmarks
from app.services import prices as price_service
from app.services.movements import DetectionParams, detect_movements
from tests import finance_helpers as fh
from tests.test_finance_ingestion import SHOCKS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import measure_detection as measure  # noqa: E402


@pytest.fixture
def data() -> fh.FactorData:
    return fh.factor_histories(300, shocks=SHOCKS)


def test_counts_use_the_detector_and_union_never_loses_a_raw_day(data):
    choice = benchmarks.choose("Technology", "Software - Infrastructure")
    counts = measure.count_symbol(data.stock, data.market.bars, data.sector.bars, choice, settings)

    window_start = data.dates[-1].toordinal() - settings.price_history_days
    raw_in_window = [
        m for m in detect_movements([b.to_point() for b in data.stock.bars], DetectionParams.from_settings())
        if m.date.toordinal() >= window_start
    ]
    assert counts.raw == len(raw_in_window)
    assert counts.union >= counts.raw and counts.union >= counts.abnormal
    assert counts.union_floor_on_abnormal >= counts.raw
    assert counts.flat_floor >= counts.raw  # raw's threshold is never below the floor
    assert counts.union - counts.raw >= 1  # the planted abnormal-only day
    assert counts.union_floor_on_abnormal > counts.union  # and the flat day, with the other basis
    assert (counts.market_benchmark, counts.sector_benchmark) == ("SPY", "XLK")


def test_without_benchmark_prices_every_mode_falls_back_to_raw(data):
    choice = benchmarks.choose("Technology", None)
    counts = measure.count_symbol(data.stock, None, None, choice, settings)
    # A day that cannot be decomposed is judged by the raw test, in any mode.
    assert counts.abnormal == counts.union == counts.raw
    assert counts.undecomposed == counts.sessions and "raw only" in counts.note


def test_the_cost_estimate_scales_the_window_to_a_year():
    counts = [
        measure.SymbolCounts("A", None, None, "SPY", None, 250, 40, 20, 10, 25, 12, 30, 0),
        measure.SymbolCounts("B", None, None, "SPY", None, 250, 30, 10, 5, 12, 6, 14, 0),
    ]
    estimate = measure.cost_estimate(counts, Decimal("0.02"))
    on_return = estimate["floor_on_return"]

    assert on_return["extra_movements_in_window"] == 7  # (25 - 20) + (12 - 10)
    assert on_return["extra_movements_per_year"] == pytest.approx(7 * 252 / 250, abs=0.05)
    assert on_return["extra_enrichments_per_night"] == pytest.approx(7 / 250, abs=0.005)
    assert on_return["one_off_backlog_usd"] == "0.1400"
    assert estimate["floor_on_abnormal"]["extra_movements_in_window"] == 14
    assert "extra_usd_per_night" not in measure.cost_estimate(counts, None)["floor_on_return"]


def test_the_command_writes_counts_profiles_and_the_estimate(data, tmp_path, monkeypatch, capsys):
    feed = {"SYNTH": data.stock, "SPY": data.market, "XLK": data.sector}

    async def fetch_many(symbols, days, *, with_profile=()):
        assert days == settings.price_fetch_days
        return {symbol: feed[symbol] for symbol in symbols}

    monkeypatch.setattr(price_service, "fetch_price_histories", fetch_many)
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"summary": {"spend_per_enrichment_usd": "0.015"}}))
    out, profiles = tmp_path / "out.json", tmp_path / "profiles.json"

    assert measure.main(["--symbols", "synth", "--usage-json", str(usage), "--json-out", str(out), "--profiles-out", str(profiles)]) == 0

    document = json.loads(out.read_text())
    assert document["counts"][0]["symbol"] == "SYNTH"
    assert document["totals"]["union"] >= document["totals"]["raw"]
    assert document["union_cost"]["spend_per_enrichment_usd"] == "0.015"
    assert document["provenance"]["settings"]["sector_map_is_provisional_default"] is True
    assert json.loads(profiles.read_text()) == {"SYNTH": {"industry": "Software - Infrastructure", "sector": "Technology"}}
    assert "| SYNTH |" in capsys.readouterr().out
