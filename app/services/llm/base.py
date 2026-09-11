"""The LLM-provider seam.

Callers depend on this interface, never on a vendor SDK. Two call shapes are
all this application needs:

* `parse_structured` -- a schema-constrained extraction, used for competitor
  resolution and relevance scoring. Structured outputs mean the pipeline never
  has to regex JSON out of prose.
* `complete` -- a plain multi-turn completion, used by the chat endpoint.

Anything vendor-specific -- SDK types, error classes, request shapes -- lives
behind an implementation of this class, the same way `news/base.py` keeps Exa
out of the ingestion code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# A single turn: {"role": "user" | "assistant", "content": ...}. Deliberately a
# plain dict -- every provider accepts this shape, and typing it to one SDK's
# param class would put the vendor back in the callers' imports.
MessageParam = dict[str, Any]


class LLMProvider(ABC):
    """Base class for LLM backends.

    Implementations raise `LLMError` for every failure, so callers can degrade
    gracefully rather than 500 the request.
    """

    #: Provider name, used in error messages and logs.
    name: str = "unknown"

    #: The model actually in use. Recorded as provenance on scored rows, so it
    #: must reflect this instance -- not a global default.
    model: str = "unknown"

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured well enough to be called."""

    @abstractmethod
    async def parse_structured(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int | None = None,
    ) -> T:
        """Return a validated `output_model` instance."""

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        max_tokens: int | None = None,
    ) -> str:
        """Return the assistant's reply as plain text."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        """Release any held connections."""
