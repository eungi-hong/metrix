"""A per-ticker data version, bumped whenever what the API shows for it changes.

`tickers.data_version` is what the read path's ETag and response cache are
keyed by (`app.services.response_cache`), so it must change on every write
that changes `GET /tickers/{symbol}`: price bars, movements, their news links
and filings, a linked article, and the ticker row itself.

Rather than trust every write site to remember, a session hook does it:

* after each flush, the tickers touched by the flushed objects are noted on
  the session (`note`): directly from `ticker_id`, or through the movement
  for links and filings, or through the links for an article;
* just before the transaction commits, each noted ticker's version is
  incremented with one UPDATE (in id order, so two transactions never wait
  on each other in opposite orders), and on Postgres a
  `NOTIFY ticker_events, '<ticker id>'` is sent, which Postgres delivers
  only if the commit succeeds (`app.services.events` streams it).

Bumping at commit rather than at flush keeps the ticker row's lock to the
last moments of the transaction, never across an LLM call. A rolled-back
transaction bumps nothing. Bulk UPDATE statements bypass the flush, so the
two in ingestion call `note` (or set the column) themselves.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import chain

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.filings import MovementFiling
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink, NewsArticle

NOTIFY_CHANNEL = "ticker_events"
_KEY = "metrix_changed"


class _Changed:
    __slots__ = ("tickers", "movements", "articles")

    def __init__(self) -> None:
        self.tickers: set[int] = set()
        self.movements: set[int] = set()
        self.articles: set[int] = set()

    def __bool__(self) -> bool:
        return bool(self.tickers or self.movements or self.articles)


def _changed(session: Session) -> _Changed:
    changed = session.info.get(_KEY)
    if changed is None:
        changed = session.info[_KEY] = _Changed()
    return changed


def note(session: Session, ticker_ids: Iterable[int | None]) -> None:
    """Mark tickers as changed in this transaction (for bulk statements).

    Takes the sync session: `async_session.sync_session`."""
    _changed(session).tickers.update(i for i in ticker_ids if i is not None)


def _collect(session: Session, objects: Iterable[object], *, deleted: bool = False) -> None:
    changed = _changed(session)
    for obj in objects:
        if isinstance(obj, (Movement, PriceBar)):
            if obj.ticker_id is not None:
                changed.tickers.add(obj.ticker_id)
        elif isinstance(obj, (MovementNewsLink, MovementFiling)):
            if obj.movement_id is not None:
                changed.movements.add(obj.movement_id)
        elif isinstance(obj, Ticker):
            if obj.id is not None:
                changed.tickers.add(obj.id)
        elif isinstance(obj, NewsArticle) and not deleted and obj.id is not None:
            changed.articles.add(obj.id)


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, flush_context) -> None:
    # After the flush, so new rows have their ids and foreign keys.
    dirty = [obj for obj in session.dirty if session.is_modified(obj, include_collections=False)]
    _collect(session, chain(session.new, dirty))
    _collect(session, session.deleted, deleted=True)


@event.listens_for(Session, "before_commit")
def _before_commit(session: Session) -> None:
    # Also fired when a savepoint is released; only the real commit bumps.
    if session.in_nested_transaction():
        return
    session.flush()  # what commit would do next; collects the last changes
    changed: _Changed | None = session.info.pop(_KEY, None)
    if not changed:
        return
    connection = session.connection()
    ids = set(changed.tickers)
    if changed.movements:
        ids.update(
            connection.execute(
                sa.select(Movement.ticker_id).where(Movement.id.in_(changed.movements))
            ).scalars()
        )
    if changed.articles:
        ids.update(
            connection.execute(
                sa.select(Movement.ticker_id)
                .join(MovementNewsLink, MovementNewsLink.movement_id == Movement.id)
                .where(MovementNewsLink.article_id.in_(changed.articles))
                .distinct()
            ).scalars()
        )
    table = Ticker.__table__
    for ticker_id in sorted(ids):
        connection.execute(
            table.update().where(table.c.id == ticker_id).values(data_version=table.c.data_version + 1)
        )
    if ids and connection.dialect.name == "postgresql":
        for ticker_id in sorted(ids):
            connection.execute(
                sa.text("SELECT pg_notify(:channel, :payload)"),
                {"channel": NOTIFY_CHANNEL, "payload": str(ticker_id)},
            )


@event.listens_for(Session, "after_transaction_end")
def _after_transaction_end(session: Session, transaction) -> None:
    # The outermost transaction is over (committed, or rolled back, in which
    # case nothing it noted happened). A savepoint ending changes nothing.
    if transaction.parent is None:
        session.info.pop(_KEY, None)
