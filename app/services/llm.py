"""Thin wrapper over the Anthropic Messages API.

Two call shapes are all this application needs:

* `parse_structured` -- a schema-constrained extraction, used for competitor
  resolution and relevance scoring. Structured outputs mean the pipeline never
  has to regex JSON out of prose.
* `complete` -- a plain multi-turn completion, used by the chat endpoint.

Every failure is re-raised as `LLMError` so callers can degrade gracefully
rather than 500 the request.
"""

from __future__ import annotations

from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from app.core.config import settings
from app.core.errors import ConfigurationError, LLMError
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

MessageParam = dict[str, Any]


class LLMClient:
    """Async Anthropic client with uniform error handling."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.anthropic_model
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self._api_key) or self._client is not None

    def _require_client(self) -> anthropic.AsyncAnthropic:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ConfigurationError(
                "ANTHROPIC_API_KEY is not set; relevance scoring and chat are unavailable."
            )
        self._client = anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=settings.anthropic_timeout_seconds,
            max_retries=2,  # SDK retries 429/5xx/connection errors with backoff
        )
        return self._client

    async def parse_structured(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int | None = None,
    ) -> T:
        """Call Claude and get back a validated `output_model` instance."""
        client = self._require_client()
        try:
            response = await client.messages.parse(
                model=self.model,
                max_tokens=max_tokens or settings.anthropic_max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_model,
            )
        except anthropic.APIError as exc:
            raise LLMError("anthropic", _describe(exc)) from exc
        except Exception as exc:
            raise LLMError("anthropic", f"unexpected failure: {exc}") from exc

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("anthropic", "structured output was empty or unparseable")

        _log_usage(response, kind="structured")
        return parsed

    async def complete(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        max_tokens: int | None = None,
    ) -> str:
        """Plain completion; returns concatenated text blocks."""
        client = self._require_client()
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=max_tokens or settings.anthropic_max_tokens,
                system=system,
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise LLMError("anthropic", _describe(exc)) from exc
        except Exception as exc:
            raise LLMError("anthropic", f"unexpected failure: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMError("anthropic", "the model declined to answer this request")

        _log_usage(response, kind="complete")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMError("anthropic", "the model returned no text")
        return text

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()


def _describe(exc: anthropic.APIError) -> str:
    """A message safe to surface, without leaking keys or full payloads."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "authentication failed -- check ANTHROPIC_API_KEY"
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited"
    if isinstance(exc, anthropic.APIStatusError):
        return f"HTTP {exc.status_code}: {str(exc.message)[:200]}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not reach the Anthropic API"
    return str(exc)[:200]


def _log_usage(response: Any, kind: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:  # pragma: no cover - stubs in tests
        return
    logger.info(
        "llm_call",
        kind=kind,
        model=getattr(response, "model", None),
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
    )


_default_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Process-wide client. Overridden in tests via FastAPI dependency_overrides."""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
