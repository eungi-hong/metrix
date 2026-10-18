#!/usr/bin/env python3
"""Label movements in the terminal, a few keystrokes each.

For every snapshot without a human or auto label it shows the move, its
decomposition and session legs, the filings around it, and the candidates
with their publication times and session buckets, then asks:

    which evidence?   candidate numbers ("1 3"), filings ("f1"), "n" for no
                      clear driver, "s" to skip, "q" to save and quit
    tier?             e (company), m (peers, industry), h (macro)
    driver?           one sentence

Labels are written as `human` to evals/golden.jsonl after every answer, so
quitting loses nothing. `--propose` writes `proposed` drafts (and mechanical
`auto` labels) for every unlabelled snapshot without asking anything;
proposed labels never count in headline metrics until a person relabels them.

    python scripts/label_movements.py            # label interactively
    python scripts/label_movements.py --propose  # drafts only
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.auto_label import propose  # noqa: E402
from evals.schema import (  # noqa: E402
    GOLDEN_PATH,
    HEADLINE_SOURCES,
    SNAPSHOTS_DIR,
    Label,
    Snapshot,
    load_labels,
    load_snapshots,
    write_labels,
)

TIERS = {"e": "easy", "m": "medium", "h": "hard"}


def render(snapshot: Snapshot) -> str:
    lines = [
        f"{snapshot.symbol} {snapshot.date}  {snapshot.daily_return:+.2%}  ({snapshot.company_name or ''}; {snapshot.sector or 'unknown sector'})",
    ]
    d = snapshot.decomposition
    if d:
        lines.append(
            f"  market {d['market']:+.2%}  sector {d['sector']:+.2%}  company {d['abnormal']:+.2%}  "
            f"SAR {d['sar']:+.1f}  dominant {d['dominant']}" if d.get("sar") is not None else f"  {d}"
        )
    if snapshot.legs:
        lines.append(f"  gap {snapshot.legs['gap']:+.2%}  intraday {snapshot.legs['intraday']:+.2%}")
    for n, f in enumerate(snapshot.filings, start=1):
        lines.append(f"  [f{n}] {f.form} {', '.join(f.events) or ''} accepted {f.accepted_at} ({f.timing or '?'}) {f.accession}")
    for n, c in enumerate(snapshot.candidates, start=1):
        lines.append(f"  [{n}] ({c.timing or '?'}, {c.search_tier}) {c.published_at}  {c.title or c.url}")
        lines.append(f"       {c.source_domain or ''} {c.url}")
    if not snapshot.complete:
        lines.append(f"  note: incomplete snapshot: {'; '.join(snapshot.notes)}")
    return "\n".join(lines)


def parse_selection(text: str, snapshot: Snapshot) -> list[str] | None:
    """Accepted ids for '1 3 f2'. None if any token does not name something."""
    accepted = []
    for token in text.replace(",", " ").split():
        if token.startswith("f") and token[1:].isdigit() and 1 <= int(token[1:]) <= len(snapshot.filings):
            accepted.append(snapshot.filings[int(token[1:]) - 1].accession)
        elif token.isdigit() and 1 <= int(token) <= len(snapshot.candidates):
            accepted.append(snapshot.candidates[int(token) - 1].id)
        else:
            return None
    return accepted


def label_one(snapshot: Snapshot, ask: Callable[[str], str]) -> Label | str:
    """A human label, or "skip" / "quit"."""
    while True:
        answer = ask("evidence (numbers, f#, n, s, q): ").strip().lower()
        if answer in ("q", "s"):
            return {"q": "quit", "s": "skip"}[answer]
        if answer == "n":
            driver = ask("why no clear driver (optional): ").strip() or "No clear driver among the candidates."
            return Label(symbol=snapshot.symbol, date=snapshot.date, driver=driver, tier="none",
                         no_clear_driver=True, label_source="human")
        accepted = parse_selection(answer, snapshot)
        if accepted:
            break
        print("  not understood")
    tier = ""
    while tier not in TIERS:
        tier = ask("tier (e, m, h): ").strip().lower()
    driver = ""
    while not driver:
        driver = ask("driver, one sentence: ").strip()
    return Label(symbol=snapshot.symbol, date=snapshot.date, driver=driver, tier=TIERS[tier],
                 accepted=accepted, label_source="human")


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--propose", action="store_true")
    parser.add_argument("--snapshots", type=Path, default=SNAPSHOTS_DIR)
    parser.add_argument("--labels", type=Path, default=GOLDEN_PATH)
    args = parser.parse_args(argv)

    snapshots = load_snapshots(args.snapshots)
    labels = {label.key: label for label in load_labels(args.labels)}
    todo = [s for key, s in sorted(snapshots.items()) if labels.get(key) is None or labels[key].label_source not in HEADLINE_SOURCES]
    print(f"{len(todo)} movements to label")

    for n, snapshot in enumerate(todo, start=1):
        if args.propose:
            if snapshot.key not in labels:
                labels[snapshot.key] = propose(snapshot)
            continue
        print(f"\n({n}/{len(todo)})\n{render(snapshot)}")
        outcome = label_one(snapshot, ask)
        if outcome == "quit":
            break
        if isinstance(outcome, Label):
            labels[snapshot.key] = outcome
            write_labels(labels.values(), args.labels)
    write_labels(labels.values(), args.labels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
