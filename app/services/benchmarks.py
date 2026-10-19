"""Which benchmarks a ticker's return is decomposed against.

The market factor is `BENCHMARK_MARKET_SYMBOL` (SPY). The sector factor comes
from the ticker's yfinance profile: its industry, if
`BENCHMARK_INDUSTRY_MAP_JSON` has it (Semiconductors to SMH, say), else its
sector through `BENCHMARK_SECTOR_MAP_JSON` (Technology to XLK). An unmapped
sector leaves a market-only model, and says so.

Self-inclusion: a sector ETF holds the stocks it benchmarks. NVDA is a large
weight in XLK and SMH, so part of NVDA's own move is in its "sector" factor,
which understates its company component on its biggest days. The map is
configuration so an equal-weight ETF can be used per sector instead; the
README says so rather than hiding it.

Benchmarks are data, not products: their prices are refreshed in the nightly
batch and stored like any ticker's, but they are not ranked by demand or
enriched with news by the nightly run.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, settings as default_settings


@dataclass(frozen=True, slots=True)
class BenchmarkChoice:
    market: str
    sector: str | None
    # Why there is no sector benchmark, when there is none.
    note: str | None = None


def choose(sector: str | None, industry: str | None, s: Settings | None = None) -> BenchmarkChoice:
    s = s or default_settings
    market = s.benchmark_market_symbol.strip().upper()
    if industry and industry in s.benchmark_industry_map_json:
        return BenchmarkChoice(market, s.benchmark_industry_map_json[industry])
    if sector and sector in s.benchmark_sector_map_json:
        return BenchmarkChoice(market, s.benchmark_sector_map_json[sector])
    if not sector:
        return BenchmarkChoice(market, None, "no sector in the ticker's profile; market-only model")
    return BenchmarkChoice(
        market, None, f"sector '{sector}' has no benchmark in BENCHMARK_SECTOR_MAP_JSON; market-only model"
    )


def all_symbols(s: Settings | None = None) -> list[str]:
    """Every benchmark the nightly run refreshes."""
    s = s or default_settings
    symbols = {s.benchmark_market_symbol.strip().upper()}
    symbols |= set(s.benchmark_sector_map_json.values())
    symbols |= set(s.benchmark_industry_map_json.values())
    return sorted(symbols)


def is_benchmark(symbol: str, s: Settings | None = None) -> bool:
    return symbol.strip().upper() in all_symbols(s)


def unmapped(profiles: dict[str, dict[str, str | None]], s: Settings | None = None) -> dict[str, str]:
    """Symbols whose profile would get no sector benchmark, and why.

    For checking real yfinance profiles against the map: a symbol is
    reported when it has a sector string that neither map covers.
    """
    s = s or default_settings
    problems: dict[str, str] = {}
    for symbol, profile in profiles.items():
        choice = choose(profile.get("sector"), profile.get("industry"), s)
        if choice.sector is None and profile.get("sector"):
            problems[symbol] = choice.note or "unmapped"
    return problems
