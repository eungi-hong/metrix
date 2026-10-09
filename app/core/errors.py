"""Domain exceptions.

Each maps to an HTTP status in `app.api.errors`. The point of the hierarchy is
that a single flaky upstream (news search, the LLM) degrades one *part* of a
response instead of 500-ing the whole request -- callers of the news and LLM
services catch `UpstreamError` and continue with what they have.

`permanent` tells the job queue whether trying again could help. An unknown
symbol, a missing key or a rejected key fails identically on every attempt, so
retrying only spends quota; a timeout or a 503 may well succeed next time.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


class MetrixError(Exception):
    """Base class for everything this application raises on purpose."""

    permanent: bool = False


class TickerNotFoundError(MetrixError):
    """The symbol does not resolve to a tradeable instrument."""

    permanent = True

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"No price data available for ticker '{symbol}'.")


class SymbolNotListed(TickerNotFoundError):
    """Not in the US symbol directory, so refused before any external call."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        MetrixError.__init__(
            self,
            f"'{symbol}' is not a listed US symbol. (Foreign listings, such as RY.TO, "
            "are served only if added to SYMBOL_ALLOWLIST.)",
        )


class UpstreamError(MetrixError):
    """A third-party dependency failed. Usually recoverable / partial."""

    def __init__(self, provider: str, message: str, *, permanent: bool = False) -> None:
        self.provider = provider
        self.permanent = permanent
        super().__init__(f"{provider}: {message}")


class PriceDataError(UpstreamError):
    """yfinance returned nothing usable."""


class NewsProviderError(UpstreamError):
    """The news search provider failed or is not configured."""


class LLMError(UpstreamError):
    """An LLM call failed, or returned something unparseable."""


def is_permanent(error: BaseException) -> bool:
    """Whether retrying could possibly help. See `MetrixError.permanent`."""
    return isinstance(error, MetrixError) and error.permanent


class AuthenticationError(MetrixError):
    """No credentials, or credentials that do not identify an active user. 401."""

    permanent = True


class ConfigurationError(MetrixError):
    """A required API key or setting is missing."""

    permanent = True


class SpendCapReached(Exception):
    """A billable call was refused: today's spend cap, or background's share of it.

    Deliberately not a `MetrixError`. The pipeline catches `MetrixError` to
    degrade gracefully, recording a failed search or scoring pass against the
    movement and moving on. A cap is not a failure of the work, and recording
    it as one would use up the movement's attempts and mark it FAILED. So this
    propagates past those handlers to the boundary, where each caller does the
    right thing with it: the worker holds the job until `retry_at` without
    using an attempt, the tickers route serves stored data with a warning, and
    chat answers 503 with `Retry-After`.
    """

    def __init__(self, call_class: str, retry_at: datetime, detail: str) -> None:
        self.call_class = call_class
        self.retry_at = retry_at
        super().__init__(detail)


class RateLimited(Exception):
    """A caller is over one of their quotas. 429, with Retry-After and the
    X-RateLimit-* headers from `result` (a `limits.LimitResult`).

    Not a `MetrixError`, for the same reason as `SpendCapReached`: it must
    reach the HTTP layer, not be absorbed by a degrade-gracefully handler.
    """

    def __init__(self, quota: str, result: Any, detail: str | None = None) -> None:
        self.quota = quota
        self.result = result
        super().__init__(
            detail
            or f"Over the {quota} quota ({result.limit}); retry in "
            f"{math.ceil(result.retry_after)} s."
        )
