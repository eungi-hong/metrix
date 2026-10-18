"""The files the harness reads and writes: snapshots, labels, results.

A snapshot freezes what the scorer saw for one movement: the movement, its
decomposition and legs, the peer set, and every candidate article with its
publication time and session bucket, plus the filings around it. A label
names which of those candidates (by id, by URL pattern, or by filing
accession number) explain the move.

Only `human` and `auto` labels count in headline metrics. `proposed` labels
are suggestions waiting for a person.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.models.news import url_fingerprint

EVALS_DIR = Path(__file__).resolve().parent
SNAPSHOTS_DIR = EVALS_DIR / "snapshots"
GOLDEN_PATH = EVALS_DIR / "golden.jsonl"
RESULTS_DIR = EVALS_DIR / "results"
RECORDINGS_DIR = EVALS_DIR / "recordings"
BASELINE_PATH = EVALS_DIR / "baseline.json"

LabelSource = Literal["human", "auto", "proposed"]
LabelTier = Literal["easy", "medium", "hard", "none"]
HEADLINE_SOURCES: frozenset[str] = frozenset({"human", "auto"})


def candidate_id(url: str) -> str:
    """Stable across snapshots: derived from the normalised URL."""
    return "c" + url_fingerprint(url)[:12]


class SnapshotCandidate(BaseModel):
    id: str
    url: str
    title: str | None = None
    source_domain: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    content: str | None = None
    search_tier: str
    timing: str | None = None
    timing_precision: str | None = None


class SnapshotFiling(BaseModel):
    accession: str
    form: str
    items: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    accepted_at: datetime | None = None
    url: str | None = None
    timing: str | None = None
    timing_precision: str | None = None


class Snapshot(BaseModel):
    symbol: str
    date: date
    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    daily_return: float
    direction: str
    window_mode: str | None = None
    peers: dict = Field(default_factory=dict)
    decomposition: dict | None = None
    legs: dict | None = None
    candidates: list[SnapshotCandidate]
    filings: list[SnapshotFiling] = Field(default_factory=list)
    # False when some tier's search could not be rebuilt from the cache.
    complete: bool = True
    notes: list[str] = Field(default_factory=list)
    built_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.symbol}_{self.date.isoformat()}"


class Label(BaseModel):
    symbol: str
    date: date
    driver: str = Field(description="One sentence: what moved the stock.")
    tier: LabelTier
    accepted: list[str] = Field(
        default_factory=list,
        description="Candidate ids, URL patterns (fnmatch), or filing accession numbers.",
    )
    no_clear_driver: bool = False
    label_source: LabelSource
    note: str | None = None

    @property
    def key(self) -> str:
        return f"{self.symbol}_{self.date.isoformat()}"

    def accepts(self, *, candidate_id: str | None = None, url: str | None = None, accession: str | None = None) -> bool:
        for item in self.accepted:
            if candidate_id and item == candidate_id:
                return True
            if accession and item == accession:
                return True
            if url and ("*" in item or "?" in item) and fnmatch.fnmatch(url, item):
                return True
            if url and item == url:
                return True
        return False


def load_snapshot(path: Path) -> Snapshot:
    return Snapshot.model_validate_json(path.read_text())


def load_snapshots(directory: Path = SNAPSHOTS_DIR) -> dict[str, Snapshot]:
    return {s.key: s for s in (load_snapshot(p) for p in sorted(directory.glob("*.json")))}


def save_snapshot(snapshot: Snapshot, directory: Path = SNAPSHOTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot.key}.json"
    path.write_text(snapshot.model_dump_json(indent=2) + "\n")
    return path


def load_labels(path: Path = GOLDEN_PATH) -> list[Label]:
    if not path.is_file():
        return []
    return [Label.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def write_labels(labels: Iterable[Label], path: Path = GOLDEN_PATH) -> None:
    ordered = sorted(labels, key=lambda label: (label.symbol, label.date))
    path.write_text("".join(json.dumps(label.model_dump(mode="json")) + "\n" for label in ordered))
