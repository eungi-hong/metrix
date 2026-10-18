"""The LLM-provider seam.

Callers depend on this interface, never on a vendor SDK. Two call shapes are
all this application needs:

* `parse_structured` -- a schema-constrained extraction, used for competitor
  resolution and relevance scoring. Structured outputs mean the pipeline never
  has to regex JSON out of prose.
* `complete` -- a plain multi-turn completion.
* `tool_turn` -- one assistant turn that may call tools, streaming its text
  as it is generated. Ask Metrix runs these in a bounded loop.

Both take `operation`, what the call is for ("relevance", "peers", "chat"),
which the spend ledger records. It is required, so a new call site cannot
leave its cost unattributed.

Anything vendor-specific -- SDK types, error classes, request shapes -- lives
behind an implementation of this class, the same way `news/base.py` keeps Exa
out of the ingestion code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# A single turn: {"role": "user" | "assistant", "content": ...}. Deliberately a
# plain dict -- every provider accepts this shape, and typing it to one SDK's
# param class would put the vendor back in the callers' imports.
MessageParam = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model may call: a name, what it does, and a JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model's arguments were not valid JSON; the call is answered
    # with an error rather than run.
    error: str | None = None


@dataclass(slots=True)
class ToolTurn:
    """One assistant turn: its text, and the tools it asked for (if any)."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


# A provider-neutral transcript for tool use. Items are dicts:
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
# Each provider converts it to its own wire shape.
TextCallback = Callable[[str], Awaitable[None]]


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
        operation: str,
        max_tokens: int | None = None,
    ) -> T:
        """Return a validated `output_model` instance."""

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        operation: str,
        max_tokens: int | None = None,
    ) -> str:
        """Return the assistant's reply as plain text."""

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        tools: list[ToolSpec],
        operation: str,
        max_tokens: int | None = None,
        on_text: TextCallback | None = None,
    ) -> ToolTurn:
        """One assistant turn with `tools` available (none: answer only).

        Text is passed to `on_text` as it streams, when given. Metered and
        rate limited like every other call, so the spend cap is checked before
        it is made. A provider without tool support raises LLMError.
        """
        from app.core.errors import LLMError

        raise LLMError(self.name, "this provider does not support tool use")

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        """Release any held connections."""
