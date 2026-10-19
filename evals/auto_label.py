"""Labels a machine may assign: only where the evidence is mechanical.

One rule, from the brief: a session whose earnings 8-K (Item 2.02) was
accepted overnight, after the prior close and before the open, is explained
by that release. The label names the filing's accession number, so a scorer
gets credit only for that primary source (Phase 3 adds filings as
candidates; until then such labels measure what news coverage achieves).

Anything a person would have to judge is never `auto`; `propose` drafts
those as `proposed`, which no headline metric counts.
"""

from __future__ import annotations

from evals.schema import Label, Snapshot

EARNINGS_ITEM = "2.02"


def auto_label(snapshot: Snapshot) -> Label | None:
    for filing in snapshot.filings:
        if (
            filing.form.startswith("8-K")
            and EARNINGS_ITEM in filing.items
            and filing.timing == "overnight"
            and filing.timing_precision == "time"
        ):
            return Label(
                symbol=snapshot.symbol,
                date=snapshot.date,
                driver=f"Earnings release (8-K Item 2.02, {filing.accession}) accepted after the prior close.",
                tier="easy",
                accepted=[filing.accession],
                label_source="auto",
            )
    return None


def propose(snapshot: Snapshot) -> Label:
    """A draft for a person to confirm or replace: the auto rule if it
    applies, else the company-tier candidates published before the move."""
    auto = auto_label(snapshot)
    if auto is not None:
        return auto
    causal = {"before_prior_close", "overnight", "intraday"}
    picks = [c.id for c in snapshot.candidates if c.search_tier == "easy" and c.timing in causal][:2]
    return Label(
        symbol=snapshot.symbol,
        date=snapshot.date,
        driver="PROPOSED: unconfirmed; review the candidates.",
        tier="easy" if picks else "none",
        accepted=picks,
        no_clear_driver=not picks,
        label_source="proposed",
    )
