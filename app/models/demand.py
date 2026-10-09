"""What users ask for: the signal pre-warming spends its budget on.

Demand has a table of its own rather than columns on `tickers`. It is written
on every request, so it is the hottest row in the system, and the ticker row
is the one ingestion claims with a conditional UPDATE; keeping them apart
means a page view never queues behind an ingestion's row lock. It is also
keyed by symbol, not ticker id, because a symbol is demanded before it has a
ticker row, and it keeps `tickers.updated_at` meaning "the data changed"
rather than "someone looked".
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TickerDemand(Base):
    """Exponentially decayed request count per symbol.

    `popularity` is stored as of `popularity_updated_at` and decays from
    there: `app.services.demand` brings it forward to "now" both when a hit
    is recorded and when the universe is ranked.
    """

    __tablename__ = "ticker_demand"

    symbol: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    popularity: Mapped[float] = mapped_column(sa.Float, nullable=False)
    popularity_updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    request_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    first_requested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    last_requested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
