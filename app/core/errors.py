"""Domain exceptions.

Each maps to an HTTP status in `app.api.errors`. The point of the hierarchy is
that a single flaky upstream (news search, the LLM) degrades one *part* of a
response instead of 500-ing the whole request -- callers of the news and LLM
services catch `UpstreamError` and continue with what they have.
"""

from __future__ import annotations


class MetrixError(Exception):
    """Base class for everything this application raises on purpose."""


class TickerNotFoundError(MetrixError):
    """The symbol does not resolve to a tradeable instrument."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"No price data available for ticker '{symbol}'.")


class UpstreamError(MetrixError):
    """A third-party dependency failed. Usually recoverable / partial."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"{provider}: {message}")


class PriceDataError(UpstreamError):
    """yfinance returned nothing usable."""


class NewsProviderError(UpstreamError):
    """The news search provider failed or is not configured."""


class LLMError(UpstreamError):
    """Anthropic call failed, or returned something unparseable."""


class ConfigurationError(MetrixError):
    """A required API key or setting is missing."""
