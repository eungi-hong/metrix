"""SQLAlchemy models.

Imported as a package so that every mapper is registered before SQLAlchemy
configures relationships, and so Alembic autogenerate sees the full metadata.
"""

from app.db.base import Base
from app.models.chat import ChatMessage, Conversation
from app.models.enums import (
    Direction,
    IngestStatus,
    MessageRole,
    NewsStatus,
    RelevanceTier,
)
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import (
    MovementNewsLink,
    NewsArticle,
    NewsQueryCache,
    normalize_url,
    url_fingerprint,
)

__all__ = [
    "Base",
    "ChatMessage",
    "Conversation",
    "Direction",
    "IngestStatus",
    "MessageRole",
    "Movement",
    "MovementNewsLink",
    "NewsArticle",
    "NewsQueryCache",
    "NewsStatus",
    "PriceBar",
    "RelevanceTier",
    "Ticker",
    "normalize_url",
    "url_fingerprint",
]
