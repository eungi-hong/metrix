"""Phase 3: filings stored and attached to movements, and scorer v2 (the
decomposition, session legs, timing buckets, filings as candidates, source
quality, roles and abstention). Synthetic data, SQLite, no network: EDGAR is
replaced by hand-built `edgar.Filing` values, and the model by a stub."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel

from app.api.routes.tickers import _filing_out
from app.core.config import settings
from app.models.filings import Filing, MovementFiling
from app.models.enums import NewsStatus
from app.models.market import Movement
from app.models.news import MovementNewsLink
from app.services import edgar, filings, ingestion, relevance_v2
from app.services.news.base import NewsCandidate, NewsProvider, NewsSearchRequest
from app.services.news.queries import MovementContext
from app.services.peers import PeerSet
from app.services.relevance_v2 import AssessmentV2, MovementFacts, RelevanceReportV2
from tests import finance_helpers as fh

UTC = timezone.utc
T = date(2024, 5, 23)  # a Thursday; P = 2024-05-22, close 20:00 UTC; T opens 13:30 UTC


def context(**overrides: Any) -> MovementContext:
    values = dict(
        symbol="NVDA", company_name="NVIDIA", sector="Technology", industry="Semiconductors",
        movement_date=T, daily_return=0.093, direction="up",
    )
    values.update(overrides)
    return MovementContext(**values)


def candidate(url: str, published: datetime | None, **kw: Any) -> NewsCandidate:
    return NewsCandidate(url=url, title=kw.pop("title", url), published_at=published, summary="text", **kw)


# -- describe_facts and source quality ---------------------------------------


def test_facts_become_shares_of_the_move_and_the_dominant_leg():
    lines = relevance_v2.describe_facts(
        MovementFacts(market=0.01, sector=0.01, company=0.08, gap=0.08, intraday=0.013), 0.10
    )
    assert "80% company-specific, 10% sector, 10% market" in lines[0]
    assert "most of the move happened at the open" in lines[1]
    assert relevance_v2.describe_facts(MovementFacts(gap=0.01, intraday=0.05), 0.06) == [
        "  Session legs: most of the move happened during the session (gap +1.00%, intraday +5.00%)"
    ]
    assert "split" in relevance_v2.describe_facts(MovementFacts(gap=0.03, intraday=0.03), 0.06)[0]
    assert relevance_v2.describe_facts(None, 0.05) == []


@pytest.mark.parametrize(
    ("url", "source_type", "quality"),
    [
        ("https://www.sec.gov/Archives/x.htm", "filing", "primary"),
        ("https://www.businesswire.com/news/1", "news", "primary"),
        ("https://ir.example.com/release", "news", "primary"),
        ("https://www.reuters.com/markets/1", "news", "tier1"),
        ("https://notreuters.com/1", "news", "other"),
        ("https://blog.example.net/1", "news", "other"),
    ],
)
def test_source_quality(url, source_type, quality):
    assert relevance_v2.source_quality(candidate(url, None, source_type=source_type)) == quality


# -- the request -------------------------------------------------------------


def test_the_prompt_carries_timing_buckets_facts_and_source_type():
    request = relevance_v2.build_scoring_request_v2(
        context(),
        [
            ("company", candidate("https://www.reuters.com/a", datetime(2024, 5, 22, 20, 25, tzinfo=UTC))),
            ("filing", candidate("https://www.sec.gov/f", datetime(2024, 5, 22, 20, 20, tzinfo=UTC), source_type="filing", accession="1")),
            ("company", candidate("https://x.test/later", datetime(2024, 5, 24, 15, 0, tzinfo=UTC))),
            ("company", candidate("https://x.test/undated", None)),
        ],
        PeerSet(),
        facts=MovementFacts(market=0.005, sector=0.01, company=0.078, gap=0.08, intraday=0.013),
    )
    user = request.user
    assert "Decomposition: about 84% company-specific" in user
    assert "most of the move happened at the open" in user
    assert "published: 2024-05-22 16:25 New York | timing: overnight" in user
    assert "source_type: filing | quality: primary" in user
    assert "timing: later" in user and "timing: unknown" in user
    assert "CANNOT have caused" in request.system
    assert [t.timing for t in request.timings] == ["overnight", "overnight", "later", None]


def test_an_empty_candidate_set_builds_no_request():
    assert relevance_v2.build_scoring_request_v2(context(), [], PeerSet(), facts=None) is None


# -- applying the verdict ----------------------------------------------------


def request_for(*published: datetime | None) -> relevance_v2.ScoringRequestV2:
    return relevance_v2.build_scoring_request_v2(
        context(), [("company", candidate(f"https://x.test/{i}", p)) for i, p in enumerate(published)], PeerSet(), facts=None
    )


def assess(index: int, score: float, tier: str = "easy", role: str = "primary") -> AssessmentV2:
    return AssessmentV2(index=index, tier=tier, score=score, role=role, rationale=f"r{index}")


def test_later_links_are_never_kept_whatever_the_score():
    request = request_for(datetime(2024, 5, 22, 21, tzinfo=UTC), datetime(2024, 5, 24, 15, tzinfo=UTC))
    outcome = relevance_v2.apply_scoring_result_v2(
        request, RelevanceReportV2(assessments=[assess(0, 0.7), assess(1, 0.99)], abstain=False), scored_by="m"
    )
    assert [s.candidate.url for s in outcome.scored] == ["https://x.test/0"]
    assert outcome.scored[0].role == "primary" and outcome.scored[0].timing == "overnight"
    assert outcome.abstained is False


def test_irrelevant_low_and_out_of_range_assessments_are_dropped_and_the_rest_sorted():
    request = request_for(*(datetime(2024, 5, 23, 15, tzinfo=UTC),) * 4)
    report = RelevanceReportV2(
        assessments=[
            assess(0, 0.5, role="contributing"), assess(1, 0.9, tier="irrelevant"), assess(2, 0.2),
            assess(3, 0.8, tier="medium", role="context"), assess(9, 0.99),
        ],
        abstain=False,
    )
    outcome = relevance_v2.apply_scoring_result_v2(request, report, scored_by="m")
    assert [(s.candidate.url, s.role) for s in outcome.scored] == [
        ("https://x.test/3", "context"), ("https://x.test/0", "contributing")
    ]


def test_abstention_links_nothing_and_says_why():
    request = request_for(datetime(2024, 5, 23, 15, tzinfo=UTC))
    outcome = relevance_v2.apply_scoring_result_v2(
        request,
        RelevanceReportV2(assessments=[assess(0, 0.95)], abstain=True, abstain_reason=" Nothing names a cause. "),
        scored_by="m",
    )
    assert (outcome.scored, outcome.abstained, outcome.abstain_reason) == ([], True, "Nothing names a cause.")
    nothing = relevance_v2.apply_scoring_result_v2(
        request, RelevanceReportV2(assessments=[assess(0, 0.1)], abstain=False), scored_by="m"
    )
    assert nothing.abstained and nothing.abstain_reason == "nothing scored above the threshold"


# -- filings attached to movements -------------------------------------------

COMPANY_DAY = 250


@pytest.fixture
def data() -> fh.FactorData:
    return fh.factor_histories(300, shocks={COMPANY_DAY: (0.0, 0.0, -0.09)})


async def stored_movement(session, data: fh.FactorData) -> Movement:
    for history in (data.market, data.sector):
        bench = await ingestion.get_or_create_ticker(session, history.profile.symbol)
        await ingestion.store_benchmark_prices(session, bench, history)
    ticker = await ingestion.get_or_create_ticker(session, data.stock.profile.symbol)
    await ingestion.refresh_prices(session, ticker, data.stock)
    await session.commit()
    return await session.scalar(sa.select(Movement).where(Movement.date == data.dates[COMPANY_DAY]))


def evening_before(data: fh.FactorData, hour: int = 21) -> datetime:
    """`hour` UTC on the session before the movement: after its 20:00 UTC close
    in summer, 21:00 in winter, so 21:30 and later are overnight either way."""
    return datetime.combine(data.dates[COMPANY_DAY - 1], datetime.min.time(), tzinfo=UTC) + timedelta(hours=hour, minutes=30)


def parsed(accession: str, accepted: datetime, items: tuple[str, ...] = ("2.02", "9.01"), form: str = "8-K") -> edgar.Filing:
    return edgar.Filing(
        cik=999, accession=accession, form=form, filing_date=accepted.date(), accepted_at=accepted,
        items=items, primary_document="doc.htm",
    )


async def test_filings_in_the_window_are_attached_with_their_timing(session, data):
    movement = await stored_movement(session, data)
    ticker = await ingestion.get_ticker(session, "SYNTH")
    inside = evening_before(data)
    far = inside - timedelta(days=30)
    for accession, accepted in (("A-1", inside), ("A-2", far)):
        session.add(
            Filing(ticker_id=ticker.id, cik=999, accession=accession, form="8-K", filing_date=accepted.date(),
                   accepted_at=accepted, items=["2.02"])
        )
    await session.flush()

    attached = await filings.attach_filings(session, ticker, movement)
    again = await filings.attach_filings(session, ticker, movement)

    assert [(a.filing.accession, a.timing, a.timing_precision) for a in attached] == [("A-1", "overnight", "time")]
    assert [a.id for a in again] == [a.id for a in attached]
    assert await session.scalar(sa.select(sa.func.count()).select_from(MovementFiling)) == 1


async def test_with_edgar_enabled_the_cik_and_new_filings_are_stored_once(session, data, monkeypatch):
    movement = await stored_movement(session, data)
    ticker = await ingestion.get_ticker(session, "SYNTH")
    monkeypatch.setattr(settings, "sec_edgar_enabled", True)
    calls: list[str] = []

    async def cik_for(symbol, *, session=None, client=None):
        calls.append(f"cik:{symbol}")
        return 999

    async def filings_for(cik, *, session=None, client=None):
        calls.append(f"filings:{cik}")
        return [parsed("B-1", evening_before(data)), parsed("B-2", evening_before(data) - timedelta(days=90))]

    monkeypatch.setattr(edgar, "cik_for", cik_for)
    monkeypatch.setattr(edgar, "filings_for", filings_for)

    attached = await filings.attach_filings(session, ticker, movement)
    await filings.attach_filings(session, ticker, movement)

    assert ticker.cik == 999
    assert calls == ["cik:SYNTH", "filings:999", "filings:999"]  # the CIK is looked up once
    assert [a.filing.accession for a in attached] == ["B-1"]
    assert attached[0].filing.url == "https://www.sec.gov/Archives/edgar/data/999/B1/doc.htm"
    assert await session.scalar(sa.select(sa.func.count()).select_from(Filing)) == 2


async def test_an_unreachable_edgar_never_fails_the_attachment(session, data, monkeypatch):
    movement = await stored_movement(session, data)
    ticker = await ingestion.get_ticker(session, "SYNTH")
    monkeypatch.setattr(settings, "sec_edgar_enabled", True)

    async def broken(symbol, *, session=None, client=None):
        raise edgar.EdgarError("unreachable")

    monkeypatch.setattr(edgar, "cik_for", broken)
    assert await filings.attach_filings(session, ticker, movement) == []


# -- enrichment under scorer v2 ----------------------------------------------


class OneArticle(NewsProvider):
    name = "one"

    def __init__(self, published: datetime) -> None:
        self.published = published

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        return {"results": [1] if "company news" in request.query else []}

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        return [
            NewsCandidate(url="https://www.reuters.com/synth", title="Synth cuts guidance", published_at=self.published,
                          summary="Guidance cut.", provider=self.name)
            for _ in raw["results"]
        ]


class V2Stub:
    """Answers peer and v2 relevance requests; `abstain` flips the verdict."""

    name = "stub"
    model = "stub-v2"
    available = True

    def __init__(self) -> None:
        self.abstain = False
        self.users: list[str] = []

    async def parse_structured(self, *, system: str, user: str, output_model: type[BaseModel], **kw: Any) -> Any:
        if output_model is PeerSet:
            return PeerSet()
        assert output_model is RelevanceReportV2
        self.users.append(user)
        count = user.count("] title:")
        if self.abstain:
            return RelevanceReportV2(assessments=[assess(i, 0.1, tier="irrelevant") for i in range(count)],
                                     abstain=True, abstain_reason="No candidate names a cause.")
        return RelevanceReportV2(
            assessments=[assess(i, 0.92 - 0.2 * i, role="primary" if i == 0 else "contributing") for i in range(count)],
            abstain=False,
        )


async def test_v2_scores_filings_with_the_news_and_records_roles(session, data, monkeypatch):
    monkeypatch.setattr(settings, "relevance_scorer_version", "v2")
    movement = await stored_movement(session, data)
    ticker = await ingestion.get_ticker(session, "SYNTH")
    session.add(Filing(ticker_id=ticker.id, cik=999, accession="C-1", form="8-K",
                       filing_date=evening_before(data).date(), accepted_at=evening_before(data, 20), items=["2.02"]))
    await session.commit()
    llm = V2Stub()

    await ingestion.enrich_movement(session, movement.id, llm=llm, news_provider=OneArticle(evening_before(data, 22)))

    user = llm.users[0]
    assert "source_type: filing | quality: primary" in user and "Item 2.02 results of operations" in user
    assert "Decomposition: about" in user
    link = await session.scalar(sa.select(MovementNewsLink))
    attachment = await session.scalar(sa.select(MovementFiling).execution_options(populate_existing=True))
    roles = {link.role, attachment.role}
    assert roles == {"primary", "contributing"} and link.scorer_version == "v2"
    assert attachment.relevance_score is not None and attachment.scored_by == "stub-v2"
    fresh = await session.scalar(sa.select(Movement).where(Movement.id == movement.id).execution_options(populate_existing=True))
    assert fresh.evidence_abstained is False and fresh.abstain_reason is None

    # A later pass that abstains supersedes both, keeps them, and says why.
    llm.abstain = True
    await session.execute(sa.update(Movement).where(Movement.id == movement.id).values(news_status=NewsStatus.PENDING))
    await session.commit()
    await ingestion.enrich_movement(session, movement.id, llm=llm, news_provider=OneArticle(evening_before(data, 22)))
    link = await session.scalar(sa.select(MovementNewsLink).execution_options(populate_existing=True))
    attachment = await session.scalar(sa.select(MovementFiling).execution_options(populate_existing=True))
    fresh = await session.scalar(sa.select(Movement).where(Movement.id == movement.id).execution_options(populate_existing=True))
    assert link.superseded_at is not None and attachment.superseded_at is not None
    assert (fresh.evidence_abstained, fresh.abstain_reason) == (True, "No candidate names a cause.")
    out = _filing_out(attachment)
    assert out.accession == "C-1" and out.events == ["results of operations (earnings)"]
    assert out.role is None and out.relevance_score is None  # a superseded verdict is not shown


async def test_v1_attaches_filings_but_does_not_score_them(session, data, stub_llm):
    movement = await stored_movement(session, data)
    ticker = await ingestion.get_ticker(session, "SYNTH")
    session.add(Filing(ticker_id=ticker.id, cik=999, accession="D-1", form="8-K",
                       filing_date=evening_before(data).date(), accepted_at=evening_before(data), items=["2.02"]))
    await session.commit()

    await ingestion.enrich_movement(session, movement.id, llm=stub_llm, news_provider=OneArticle(evening_before(data)))

    attachment = await session.scalar(sa.select(MovementFiling))
    assert attachment.timing == "overnight" and attachment.relevance_score is None
    assert all("source_type: filing" not in call["user"] for call in stub_llm.structured_calls)
    link = await session.scalar(sa.select(MovementNewsLink))
    assert link.scorer_version == "v1" and link.role is None


async def test_a_filing_another_worker_stored_first_does_not_fail_the_attachment(session, data, monkeypatch):
    movement = await stored_movement(session, data)
    ticker = await ingestion.get_ticker(session, "SYNTH")
    monkeypatch.setattr(settings, "sec_edgar_enabled", True)
    ticker.cik = 999
    session.add(Filing(ticker_id=ticker.id, cik=999, accession="E-1", form="8-K",
                       filing_date=evening_before(data).date(), accepted_at=evening_before(data), items=["2.02"]))
    await session.flush()
    real_scalars = session.scalars

    async def blind_once(statement, *a, **kw):
        # The known-accessions read misses E-1, as if it were stored after it.
        result = await real_scalars(statement, *a, **kw)
        if "filings.accession" in str(statement) and "movement" not in str(statement):
            monkeypatch.setattr(session, "scalars", real_scalars)
            return type("Empty", (), {"all": staticmethod(lambda: [])})()
        return result

    async def filings_for(cik, *, session=None, client=None):
        return [parsed("E-1", evening_before(data)), parsed("E-2", evening_before(data) + timedelta(minutes=5))]

    monkeypatch.setattr(edgar, "filings_for", filings_for)
    monkeypatch.setattr(session, "scalars", blind_once)

    attached = await filings.attach_filings(session, ticker, movement)

    assert sorted(a.filing.accession for a in attached) == ["E-1", "E-2"]
    assert await session.scalar(sa.select(sa.func.count()).select_from(Filing)) == 2
