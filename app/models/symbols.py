"""The directory of listed US symbols, to refuse nonsense before it costs anything.

`symbol` is canonical: Yahoo's form, which is what ingestion fetches with.
The directory files write class shares with a dot (`BRK.B`); Yahoo writes a
hyphen (`BRK-B`). `app.services.symbol_directory` converts.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ListedSymbol(Base):
    __tablename__ = "listed_symbols"

    symbol: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(sa.String(400))
    # The listing exchange's code in the source file ("Q" for Nasdaq, "N" NYSE, ...).
    exchange: Mapped[str | None] = mapped_column(sa.String(8))
    is_etf: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # Which file it came from: "nasdaqlisted" or "otherlisted".
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
