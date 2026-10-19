#!/usr/bin/env python3
"""Count movements per detection mode on real prices, and what union would cost.

The README's detection table and the decision to make `union` the default
both need real numbers: how many days each mode flags, and how many extra
enrichments union would buy. This fetches prices and profiles from yfinance
through the app's own price service (rate limited, no keys), runs the same
detection code ingestion runs (`app.services.detection`,
`app.services.analytics`), and counts, per symbol, over the display window
(the last PRICE_HISTORY_DAYS calendar days):

    flat 2%     |r| >= MOVEMENT_FLOOR_PCT alone (the README's comparison)
    raw         the v1 detector, max(floor, k * sigma)
    abnormal    |SAR| >= MOVEMENT_ABNORMAL_K and the floor, on the day's return
    union       raw or abnormal
    abnormal*   and union*: the floor on the abnormal return instead
                (MOVEMENT_ABNORMAL_FLOOR_BASIS=abnormal)

A day that cannot be decomposed (too little history, a missing benchmark
bar) is judged by the raw test in every mode, as ingestion does; the last
column counts those days.

With `--usage-json` (the output of `baseline_remote.py usage`) it also
estimates what union adds: the extra movements a year, per night, and their
cost at the measured spend per enrichment. Every number here is real data on
the day it runs; nothing is stored.

    # The README table
    python scripts/measure_detection.py --symbols NVDA,KO,JNJ --json-out detection_readme.json

    # Union's cost, for both seed lists, and the real profile strings
    python scripts/measure_detection.py --symbols NVDA,AAPL,TSLA,MSFT,AMZN \\
        --usage-json usage.json --json-out detection_production5.json
    python scripts/measure_detection.py --seed-file data/seed_universe.txt \\
        --usage-json usage.json --json-out detection_local40.json \\
        --profiles-out tests/fixtures/yfinance_profiles.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import PROVISIONAL_SECTOR_BENCHMARKS, Settings, settings  # noqa: E402
from app.services import analytics, benchmarks, detection  # noqa: E402
from app.services import prices as price_service  # noqa: E402
from app.services.movements import DetectionParams, compute_returns  # noqa: E402

TRADING_DAYS_PER_YEAR = 252


@dataclass(slots=True)
class SymbolCounts:
    symbol: str
    sector: str | None
    industry: str | None
    market_benchmark: str | None
    sector_benchmark: str | None
    sessions: int
    flat_floor: int
    raw: int
    abnormal: int
    union: int
    abnormal_floor_on_abnormal: int
    union_floor_on_abnormal: int
    undecomposed: int
    note: str | None = None


def count_symbol(
    history: price_service.PriceHistory,
    market: list[price_service.PriceBarData] | None,
    sector: list[price_service.PriceBarData] | None,
    choice: benchmarks.BenchmarkChoice,
    s: Settings,
) -> SymbolCounts:
    """Counts for one symbol over its display window. Pure."""
    params = DetectionParams.from_settings(s)
    bars = sorted(history.bars, key=lambda b: b.date)
    returns = compute_returns([b.to_point() for b in bars], params)
    display_start = bars[-1].date - timedelta(days=s.price_history_days)
    window = [r for r in returns if r.date >= display_start]

    note = choice.note
    if not market:
        aligned = None
        note = f"no prices for {choice.market}; raw only"
    else:
        aligned = analytics.align_returns(bars, market, sector or None)
        if choice.sector and not sector:
            note = f"no prices for {choice.sector}; market-only model"
    analytics_params = analytics.AnalyticsParams(
        estimation_sessions=s.decomposition_estimation_sessions,
        min_sessions=s.decomposition_min_sessions,
        winsor_z=s.decomposition_winsor_z,
        rvol_lookback_sessions=s.rvol_lookback_sessions,
        drift_label_fraction=s.drift_label_fraction,
        regime_window_sessions=s.regime_window_sessions,
        regime_lookback_sessions=s.regime_lookback_sessions,
    )
    readings = analytics.abnormal_readings(aligned, bars, [r.date for r in window], analytics_params)

    def flagged(mode: str, basis: str) -> int:
        verdicts = detection.evaluate(
            window, readings, mode=mode, abnormal_k=s.movement_abnormal_k,
            floor=params.floor, floor_basis=basis,
        )
        return sum(v.qualifies for v in verdicts)

    return SymbolCounts(
        symbol=history.profile.symbol,
        sector=history.profile.sector,
        industry=history.profile.industry,
        market_benchmark=choice.market if market else None,
        sector_benchmark=choice.sector if sector else None,
        sessions=len(window),
        flat_floor=sum(abs(r.daily_return) >= params.floor for r in window),
        raw=sum(r.is_major for r in window),
        abnormal=flagged("abnormal", "return"),
        union=flagged("union", "return"),
        abnormal_floor_on_abnormal=flagged("abnormal", "abnormal"),
        union_floor_on_abnormal=flagged("union", "abnormal"),
        undecomposed=sum(readings.get(r.date) is None for r in window),
        note=note,
    )


def cost_estimate(counts: list[SymbolCounts], spend_per_enrichment: Decimal | None) -> dict[str, Any]:
    """What union adds over raw, per year and per night, and at what price.

    The display window is about a year of sessions, so its extra movements
    are scaled to 252 sessions. Each night's new session adds, on average,
    that year's extra divided by 252. Enriching the window's existing extra
    movements once is a separate, one-off backlog.
    """
    sessions = max((c.sessions for c in counts), default=0) or 1
    out: dict[str, Any] = {"symbols": len(counts), "spend_per_enrichment_usd": None if spend_per_enrichment is None else str(spend_per_enrichment)}
    for basis, union_field in (("return", "union"), ("abnormal", "union_floor_on_abnormal")):
        extra = sum(getattr(c, union_field) - c.raw for c in counts)
        per_year = extra * TRADING_DAYS_PER_YEAR / sessions
        per_night = per_year / TRADING_DAYS_PER_YEAR
        entry: dict[str, Any] = {
            "extra_movements_in_window": extra,
            "extra_movements_per_year": round(per_year, 1),
            "extra_enrichments_per_night": round(per_night, 2),
        }
        if spend_per_enrichment is not None:
            entry["extra_usd_per_night"] = str((Decimal(str(per_night)) * spend_per_enrichment).quantize(Decimal("0.0001")))
            entry["one_off_backlog_usd"] = str((extra * spend_per_enrichment).quantize(Decimal("0.0001")))
        out[f"floor_on_{basis}"] = entry
    return out


def markdown(counts: list[SymbolCounts]) -> str:
    rows = [
        "| Ticker | Sector (benchmark) | Sessions | Flat 2% | raw | abnormal | union | abnormal* | union* | Not decomposed |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in counts:
        bench = c.sector_benchmark or ("market only" if c.market_benchmark else "none")
        rows.append(
            f"| {c.symbol} | {c.sector or 'unknown'} ({bench}) | {c.sessions} | {c.flat_floor} | {c.raw} | "
            f"{c.abnormal} | {c.union} | {c.abnormal_floor_on_abnormal} | {c.union_floor_on_abnormal} | {c.undecomposed} |"
        )
    return "\n".join(rows)


def _symbols(args: argparse.Namespace) -> list[str]:
    raw: list[str] = []
    if args.symbols:
        raw += args.symbols.split(",")
    if args.seed_file:
        for line in Path(args.seed_file).read_text().splitlines():
            raw.append(line.split("#", 1)[0])
    return sorted({s.strip().upper() for s in raw if s.strip()})


async def measure(symbols: list[str], s: Settings) -> tuple[list[SymbolCounts], dict[str, Any], list[str]]:
    days = s.price_fetch_days
    fetched = await price_service.fetch_price_histories(symbols, days, with_profile=symbols)
    failed = sorted(sym for sym, h in fetched.items() if isinstance(h, Exception))
    histories = {sym: h for sym, h in fetched.items() if not isinstance(h, Exception)}
    choices = {sym: benchmarks.choose(h.profile.sector, h.profile.industry, s) for sym, h in histories.items()}
    wanted = sorted({c.market for c in choices.values()} | {c.sector for c in choices.values() if c.sector})
    bench = await price_service.fetch_price_histories(wanted, days)
    bench_bars = {sym: h.bars for sym, h in bench.items() if not isinstance(h, Exception)}

    counts = [
        count_symbol(
            histories[sym],
            bench_bars.get(choices[sym].market),
            bench_bars.get(choices[sym].sector) if choices[sym].sector else None,
            choices[sym],
            s,
        )
        for sym in sorted(histories)
    ]
    profiles = {sym: {"sector": h.profile.sector, "industry": h.profile.industry} for sym, h in histories.items()}
    return counts, profiles, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", help="comma-separated, e.g. NVDA,KO,JNJ")
    parser.add_argument("--seed-file", help="one symbol per line, '#' comments, e.g. data/seed_universe.txt")
    parser.add_argument("--usage-json", type=Path, help="baseline_remote.py usage output, for the cost estimate")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--profiles-out", type=Path, help="write {symbol: {sector, industry}} here")
    args = parser.parse_args(argv)

    symbols = _symbols(args)
    if not symbols:
        parser.error("give --symbols or --seed-file")
    counts, profiles, failed = asyncio.run(measure(symbols, settings))

    spend: Decimal | None = None
    if args.usage_json:
        value = json.loads(args.usage_json.read_text())["summary"].get("spend_per_enrichment_usd")
        spend = Decimal(value) if value is not None else None
    estimate = cost_estimate(counts, spend)
    unmapped = benchmarks.unmapped(profiles, settings)

    print(markdown(counts))
    print()
    print(json.dumps(estimate, indent=2))
    if failed:
        print(f"Could not fetch: {', '.join(failed)}")
    if unmapped:
        print("UNMAPPED SECTORS (market-only model): " + "; ".join(f"{k}: {v}" for k, v in sorted(unmapped.items())))

    document = {
        "provenance": {
            "label": "real data (yfinance), measured on the day it ran",
            "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "command": " ".join(["python", "scripts/measure_detection.py", *(argv if argv is not None else sys.argv[1:])]),
            "settings": {
                "movement_k": settings.movement_k,
                "movement_floor_pct": settings.movement_floor_pct,
                "movement_std_window": settings.movement_std_window,
                "movement_abnormal_k": settings.movement_abnormal_k,
                "decomposition_estimation_sessions": settings.decomposition_estimation_sessions,
                "decomposition_min_sessions": settings.decomposition_min_sessions,
                "decomposition_winsor_z": settings.decomposition_winsor_z,
                "price_history_days": settings.price_history_days,
                "sector_map_is_provisional_default": settings.benchmark_sector_map_json == PROVISIONAL_SECTOR_BENCHMARKS,
            },
        },
        "counts": [asdict(c) for c in counts],
        "totals": {
            field: sum(getattr(c, field) for c in counts)
            for field in ("sessions", "flat_floor", "raw", "abnormal", "union", "abnormal_floor_on_abnormal", "union_floor_on_abnormal", "undecomposed")
        },
        "union_cost": estimate,
        "unmapped_sectors": unmapped,
        "failed": failed,
        "profiles": profiles,
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(document, indent=2, default=str) + "\n")
        print(f"wrote {args.json_out}")
    if args.profiles_out:
        args.profiles_out.parent.mkdir(parents=True, exist_ok=True)
        args.profiles_out.write_text(json.dumps(profiles, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.profiles_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
