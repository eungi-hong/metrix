"""The evaluation harness: metrics, snapshots, replay, gate, labels, coverage.

Offline. The snapshots here are built from fixture-provider enrichments
(synthetic articles), which is what proves the snapshot rebuilds exactly the
candidates the scorer was shown.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import selectinload

from app.models.market import Movement, Ticker
from app.models.news import MovementNewsLink, NewsQueryCache
from app.services import ingestion
from evals import metrics, run
from evals.auto_label import auto_label, propose
from evals.coverage import composition
from evals.metrics import CaseResult, RankedLink
from evals.replay import RecordingLLM, ReplayMissing
from evals.schema import Label, Snapshot, SnapshotCandidate, SnapshotFiling, candidate_id, load_labels, write_labels
from evals.snapshot import build_snapshot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import label_movements  # noqa: E402

DAY = date(2025, 5, 22)


def label(**kw) -> Label:
    base = dict(symbol="NVDA", date=DAY, driver="d", tier="easy", accepted=["c1"], label_source="human")
    return Label(**(base | kw))


def link(cid: str, tier: str = "easy", score: float = 0.9, timing: str | None = "overnight") -> RankedLink:
    return RankedLink(candidate_id=cid, url=f"https://n.test/{cid}", tier=tier, score=score, timing=timing)


# ------------------------------------------------------------------ metrics


def test_hit_recall_and_tier_accuracy():
    cases = [
        CaseResult(label(accepted=["a"]), [link("a"), link("b")]),  # hit, recall, right tier
        CaseResult(label(accepted=["c"], tier="hard"), [link("x"), link("c", tier="easy")]),  # recall only, wrong tier
        CaseResult(label(accepted=["z"]), [link("x")]),  # miss
        CaseResult(label(accepted=[], no_clear_driver=True, tier="none"), []),  # correct abstention
    ]
    assert metrics.hit_at_1(cases) == pytest.approx(1 / 3)
    assert metrics.recall_at_5(cases) == pytest.approx(2 / 3)
    assert metrics.tier_accuracy(cases) == pytest.approx(1 / 2)
    assert metrics.tier_confusion(cases) == {"easy": {"easy": 1}, "hard": {"easy": 1}}
    assert metrics.abstention(cases) == (1.0, 1.0)


def test_only_the_top_five_count_for_recall():
    case = CaseResult(label(accepted=["f"]), [link(c) for c in "abcdef"])
    assert not case.recall_at_5


def test_abstention_precision_and_recall():
    cases = [
        CaseResult(label(no_clear_driver=True, accepted=[], tier="none"), []),
        CaseResult(label(no_clear_driver=True, accepted=[], tier="none"), [link("a")]),  # missed abstention
        CaseResult(label(accepted=["b"]), []),  # abstained wrongly
    ]
    assert metrics.abstention(cases) == (0.5, 0.5)


def test_timing_violations_and_calibration():
    cases = [
        CaseResult(label(accepted=["a"]), [link("a", score=0.95), link("b", score=0.7, timing="later"), link("c", score=0.4, timing="later")]),
    ]
    assert metrics.timing_violations(cases, min_score=0.35) == 2
    assert metrics.timing_violations(cases, min_score=0.5) == 1
    buckets = {b["low"]: b for b in metrics.calibration(cases)}
    assert buckets[0.9]["accepted_share"] == 1.0 and buckets[0.6]["accepted_share"] == 0.0
    assert buckets[0.35]["links"] == 1


def test_bootstrap_is_deterministic_and_brackets_the_estimate():
    cases = [CaseResult(label(accepted=["a"]), [link("a" if i % 3 else "x")]) for i in range(30)]
    first, second = metrics.bootstrap(cases), metrics.bootstrap(cases)
    assert first == second
    low, high = first["hit_at_1"]
    assert low <= metrics.hit_at_1(cases) <= high and low < high
    assert metrics.bootstrap([])["hit_at_1"] is None


def test_labels_accept_ids_url_patterns_and_accessions():
    item = label(accepted=["c123", "https://www.reuters.com/*nvidia*", "0001045810-24-000112"])
    assert item.accepts(candidate_id="c123")
    assert item.accepts(url="https://www.reuters.com/tech/nvidia-results")
    assert item.accepts(accession="0001045810-24-000112")
    assert not item.accepts(url="https://www.reuters.com/markets/other")


def test_labels_round_trip(tmp_path):
    path = tmp_path / "golden.jsonl"
    write_labels([label(symbol="ZZZ"), label(symbol="AAA")], path)
    assert [item.symbol for item in load_labels(path)] == ["AAA", "ZZZ"]


# ---------------------------------------------------------------- snapshots


async def enriched(session, stub_llm) -> tuple[Movement, Ticker]:
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)
    ticker = await ingestion.get_ticker(session, "TEST")
    movement = await session.scalar(
        sa.select(Movement).options(selectinload(Movement.news_links).selectinload(MovementNewsLink.article))
    )
    return movement, ticker


async def test_a_snapshot_rebuilds_exactly_what_the_scorer_saw(session, stub_llm):
    movement, ticker = await enriched(session, stub_llm)
    shown = stub_llm.structured_calls[-1]["user"]

    snapshot = await build_snapshot(session, movement, ticker, provider="fixture")

    assert snapshot.complete and snapshot.notes == []
    assert len(snapshot.candidates) == shown.count("] title:")
    for c in snapshot.candidates:
        assert c.title in shown
    linked = {candidate_id(item.article.url) for item in movement.news_links}
    assert linked <= {c.id for c in snapshot.candidates}


async def test_a_missing_cache_row_makes_an_incomplete_snapshot_with_the_links_kept(session, stub_llm):
    movement, ticker = await enriched(session, stub_llm)
    await session.execute(sa.delete(NewsQueryCache))
    await session.commit()

    snapshot = await build_snapshot(session, movement, ticker, provider="fixture")

    assert not snapshot.complete and any("not in the cache" in n for n in snapshot.notes)
    assert {c.id for c in snapshot.candidates} == {candidate_id(item.article.url) for item in movement.news_links}


# ------------------------------------------------------- replay, run, gate


def snapshot_with(candidates: list[str], filings: list[SnapshotFiling] | None = None) -> Snapshot:
    return Snapshot(
        symbol="NVDA", date=DAY, company_name="NVIDIA", sector="Technology", industry="Semiconductors",
        daily_return=-0.06, direction="down", peers={"peers": [], "industry_themes": []},
        candidates=[
            SnapshotCandidate(id=candidate_id(f"https://n.test/{c}"), url=f"https://n.test/{c}", title=c,
                              search_tier="easy", timing="overnight",
                              published_at=datetime(2025, 5, 21, 22, tzinfo=timezone.utc))
            for c in candidates
        ],
        filings=filings or [],
    )


async def test_recorded_answers_replay_to_the_same_metrics(tmp_path, stub_llm):
    snap = snapshot_with(["first", "second", "third"])
    gold = label(accepted=[snap.candidates[0].id])

    recorder = RecordingLLM(tmp_path, mode="record", model="stub-model", inner=stub_llm)
    live, _ = await run.evaluate([gold], {snap.key: snap}, recorder)
    replayer = RecordingLLM(tmp_path, mode="replay", model="stub-model")
    replayed, _ = await run.evaluate([gold], {snap.key: snap}, replayer)

    assert replayer.replayed == 1 and len(list(tmp_path.glob("*.json"))) == 1
    assert [item.candidate_id for item in replayed[0].ranked] == [item.candidate_id for item in live[0].ranked]
    assert replayed[0].hit_at_1  # the stub scores the first candidate 0.91
    with pytest.raises(ReplayMissing):
        await run.evaluate([gold], {snap.key: snapshot_with(["other"])}, replayer)


def test_the_gate_fails_on_a_regression_beyond_tolerance():
    baseline = {"metrics": {"hit_at_1": 0.6, "recall_at_5": 0.8, "timing_violations": 0}, "tolerance": 0.05}
    assert run.gate({"hit_at_1": 0.57, "recall_at_5": 0.8, "timing_violations": 0}, baseline) == []
    problems = run.gate({"hit_at_1": 0.5, "recall_at_5": 0.8, "timing_violations": 2}, baseline)
    assert any("hit_at_1" in p for p in problems) and any("timing" in p for p in problems)


def test_run_with_no_headline_labels_does_nothing(tmp_path, capsys):
    path = tmp_path / "golden.jsonl"
    write_labels([label(label_source="proposed")], path)
    assert run.main(["--replay", "--labels", str(path)]) == 0
    assert "0 human or auto labels" in capsys.readouterr().out


def test_live_needs_a_budget(tmp_path):
    with pytest.raises(SystemExit):
        run.main(["--live"])


async def test_run_writes_results_from_recordings(tmp_path, stub_llm, monkeypatch):
    snap = snapshot_with(["first", "second"])
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / f"{snap.key}.json").write_text(snap.model_dump_json())
    write_labels([label(accepted=[snap.candidates[0].id])], tmp_path / "golden.jsonl")
    recorder = RecordingLLM(tmp_path / "rec", mode="record", model="stub-model", inner=stub_llm)
    await run.evaluate(load_labels(tmp_path / "golden.jsonl"), {snap.key: snap}, recorder)

    code = await _run_main_in_thread(
        ["--replay", "--model", "stub-model", "--labels", str(tmp_path / "golden.jsonl"),
         "--snapshots", str(tmp_path / "snapshots"), "--recordings", str(tmp_path / "rec"),
         "--results", str(tmp_path / "results"), "--name", "t"]
    )
    assert code == 0
    document = json.loads((tmp_path / "results" / "t.json").read_text())
    assert document["metrics"]["hit_at_1"] == 1.0 and document["mode"] == "replay"
    assert document["labels"] == {"human": 1, "auto": 0, "proposed": 0}


async def _run_main_in_thread(argv: list[str]) -> int:
    import asyncio

    return await asyncio.to_thread(run.main, argv)


# ------------------------------------------------------------------- labels


def filing(timing: str, items: list[str]) -> SnapshotFiling:
    return SnapshotFiling(accession="0001045810-25-000001", form="8-K", items=items, timing=timing, timing_precision="time")


def test_an_overnight_earnings_8k_is_an_auto_label():
    auto = auto_label(snapshot_with(["a"], [filing("overnight", ["2.02", "9.01"])]))
    assert auto is not None and auto.label_source == "auto"
    assert auto.accepted == ["0001045810-25-000001"] and auto.tier == "easy"
    assert auto_label(snapshot_with(["a"], [filing("intraday", ["2.02"])])) is None
    assert auto_label(snapshot_with(["a"], [filing("overnight", ["5.02"])])) is None


def test_anything_else_is_only_proposed():
    drafted = propose(snapshot_with(["a", "b", "c"]))
    assert drafted.label_source == "proposed" and len(drafted.accepted) == 2
    assert propose(snapshot_with([])).no_clear_driver


def test_the_labelling_tool_parses_and_labels():
    snap = snapshot_with(["a", "b"], [filing("overnight", ["2.02"])])
    assert label_movements.parse_selection("2 f1", snap) == [snap.candidates[1].id, "0001045810-25-000001"]
    assert label_movements.parse_selection("9", snap) is None
    answers = iter(["xx", "1", "h", "Rate decision repriced chips."])
    made = label_movements.label_one(snap, lambda _: next(answers))
    assert made.label_source == "human" and made.tier == "hard" and made.accepted == [snap.candidates[0].id]
    assert label_movements.label_one(snap, lambda _: "n").no_clear_driver
    assert "NVDA" in label_movements.render(snap)


def test_proposing_writes_drafts_without_asking(tmp_path):
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    (snaps / "NVDA_2025-05-22.json").write_text(snapshot_with(["a"]).model_dump_json())
    golden = tmp_path / "golden.jsonl"
    label_movements.main(["--propose", "--snapshots", str(snaps), "--labels", str(golden)], ask=lambda _: pytest.fail("asked"))
    assert [item.label_source for item in load_labels(golden)] == ["proposed"]


def test_coverage_counts_what_is_labelled_against_the_targets():
    snap = snapshot_with(["a"], [filing("overnight", ["2.02"])])
    report = composition({snap.key: snap}, [label(accepted=["x"])])
    assert report["labelled"] == 1 and report["earnings_days"] == 1
    assert report["movements_short_of_target"] == 59 and report["queued_for_labelling"] == 0


async def test_scorer_v2_sees_the_filings_and_a_label_can_accept_one_by_accession(tmp_path):
    from app.services.relevance_v2 import AssessmentV2, RelevanceReportV2

    class FilingFirst:
        model = "stub-v2"
        available = True
        users: list[str] = []

        async def parse_structured(self, *, system, user, output_model, **kw):
            assert output_model is RelevanceReportV2
            self.users.append(user)
            count = user.count("] title:")
            return RelevanceReportV2(
                assessments=[
                    AssessmentV2(index=i, tier="easy", score=0.95 if "SEC filing" in user.split(f"[{i}] title:")[1].split("\n")[0] else 0.4,
                                 role="primary", rationale="r")
                    for i in range(count)
                ],
                abstain=False,
            )

    filing = SnapshotFiling(accession="0001045810-25-000001", form="8-K", items=["2.02"],
                            events=["results of operations (earnings)"], timing="overnight",
                            accepted_at=datetime(2025, 5, 21, 20, 20, tzinfo=timezone.utc))
    snap = snapshot_with(["first"], filings=[filing])
    snap.decomposition = {"market": -0.01, "sector": -0.01, "abnormal": -0.04}
    snap.legs = {"gap": -0.05, "intraday": -0.01}
    gold = label(accepted=[filing.accession])
    inner = FilingFirst()
    recorder = RecordingLLM(tmp_path, mode="record", model="stub-v2", inner=inner)

    v2, _ = await run.evaluate([gold], {snap.key: snap}, recorder, scorer="v2")

    assert v2[0].hit_at_1 and v2[0].ranked[0].accession == filing.accession
    assert v2[0].ranked[0].timing == "overnight"
    assert "Item 2.02 results of operations (earnings)" in inner.users[0] and "Decomposition: about" in inner.users[0]
    replayed, _ = await run.evaluate([gold], {snap.key: snap}, RecordingLLM(tmp_path, mode="replay", model="stub-v2"), scorer="v2")
    assert replayed[0].hit_at_1
