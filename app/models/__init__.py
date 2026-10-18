"""SQLAlchemy models.

Imported as a package so that every mapper is registered before SQLAlchemy
configures relationships, and so Alembic autogenerate sees the full metadata.
"""

from app.db.base import Base
from app.models.chat import ChatMessage, Conversation
from app.models.demand import TickerDemand
from app.models.filings import Filing, MovementFiling
from app.models.enums import (
    Direction,
    IngestStatus,
    MessageRole,
    NewsStatus,
    RelevanceTier,
)
from app.models.identity import ApiKey, Plan, User
from app.models.jobs import Job, JobKind, JobRequester, JobSource, JobStatus, PrewarmRun
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import (
    MovementNewsLink,
    NewsArticle,
    NewsQueryCache,
    normalize_url,
    url_fingerprint,
)
from app.models.symbols import ListedSymbol
from app.models.usage import SpendDaily, UsageEvent

__all__ = [
    "ApiKey",
    "Base",
    "ChatMessage",
    "Filing",
    "MovementFiling",
    "Conversation",
    "Direction",
    "IngestStatus",
    "Job",
    "JobKind",
    "JobRequester",
    "JobSource",
    "ListedSymbol",
    "JobStatus",
    "MessageRole",
    "Movement",
    "MovementNewsLink",
    "NewsArticle",
    "NewsQueryCache",
    "NewsStatus",
    "Plan",
    "PrewarmRun",
    "PriceBar",
    "RelevanceTier",
    "SpendDaily",
    "Ticker",
    "TickerDemand",
    "UsageEvent",
    "User",
    "normalize_url",
    "url_fingerprint",
]

# The data-version session hooks, registered wherever the models are loaded
# (API, worker, scripts, tests). Imported last: it needs the models above.
from app.db import versions as _versions  # noqa: E402,F401
