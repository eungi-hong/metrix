"""How far the labelled set is from the composition the brief asks for.

Targets: 60 to 80 movements across at least 12 tickers in at least 6
sectors, all three tiers, both directions, earnings and non-earnings days,
and some "no clear driver" days. This reads the snapshots (what could be
labelled) and the labels (what has been), and with
`--spend-per-enrichment` estimates what enriching the shortfall would cost.
It spends nothing.

    python -m evals.coverage --spend-per-enrichment 0.012
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal

from evals.schema import HEADLINE_SOURCES, Label, Snapshot, load_labels, load_snapshots

TARGET_MOVEMENTS = (60, 80)
TARGET_TICKERS = 12
TARGET_SECTORS = 6
# A reviewer labels a movement in about this long with the terminal tool
# (an estimate for planning, not a measurement).
MINUTES_PER_LABEL = 1.5


def composition(snapshots: dict[str, Snapshot], labels: list[Label]) -> dict:
    labelled = {label.key: label for label in labels if label.label_source in HEADLINE_SOURCES}
    proposed = sum(label.label_source == "proposed" for label in labels)
    pool = list(snapshots.values())
    done = [snapshots[key] for key in labelled if key in snapshots]
    with_earnings = sum(any(f.form.startswith("8-K") and "2.02" in f.items for f in s.filings) for s in done)
    shortfall = max(TARGET_MOVEMENTS[0] - len(labelled), 0)
    unlabelled = len(pool) - len(done)
    return {
        "snapshots": len(pool),
        "labelled": len(labelled),
        "proposed_unconfirmed": proposed,
        "queued_for_labelling": unlabelled,
        "labelling_minutes_estimate": round(unlabelled * MINUTES_PER_LABEL),
        "tickers": len({s.symbol for s in done}),
        "sectors": len({s.sector for s in done if s.sector}),
        "tiers": dict(Counter(label.tier for label in labelled.values())),
        "directions": dict(Counter(s.direction for s in done)),
        "earnings_days": with_earnings,
        "no_clear_driver": sum(label.no_clear_driver for label in labelled.values()),
        "pool_tickers": len({s.symbol for s in pool}),
        "pool_sectors": len({s.sector for s in pool if s.sector}),
        "movements_short_of_target": shortfall,
        "pool_short_of_target": max(TARGET_MOVEMENTS[0] - len(pool), 0),
        "targets": {"movements": TARGET_MOVEMENTS, "tickers": TARGET_TICKERS, "sectors": TARGET_SECTORS},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spend-per-enrichment", type=Decimal)
    args = parser.parse_args(argv)
    report = composition(load_snapshots(), load_labels())
    if args.spend_per_enrichment is not None:
        report["enrich_pool_shortfall_usd"] = str(report["pool_short_of_target"] * args.spend_per_enrichment)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
