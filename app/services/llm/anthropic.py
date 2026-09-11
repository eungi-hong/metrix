"""Anthropic implementation of the LLM seam.

The only module in the application that imports the Anthropic SDK. Everything
vendor-shaped -- client construction, SDK error classes, response block
walking -- is contained here; callers see `LLMProvider` and `LLMError`.
"""

from __future__ import annotations

from typing import Any

import anthropic

from app.core.config import settings
from app.core.errors import ConfigurationError, LLMError
from app.core.logging import get_logger
from app.services.llm.base import LLMProvider, MessageParam, T

logger = get_logger(__name__)


class AnthropicProvider(LLMProvider):
    """Async Anthropic client with uniform error handling."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.llm_model
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
            timeout=settings.llm_timeout_seconds,
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
        client = self._require_client()
        try:
            response = await client.messages.parse(
                model=self.model,
                max_tokens=max_tokens or settings.llm_max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_model,
            )
        except anthropic.APIError as exc:
            raise LLMError(self.name, self._describe(exc)) from exc
        except Exception as exc:
            raise LLMError(self.name, f"unexpected failure: {exc}") from exc

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(self.name, "structured output was empty or unparseable")

        self._log_usage(response, kind="structured")
        return parsed

    async def complete(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        max_tokens: int | None = None,
    ) -> str:
        client = self._require_client()
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=max_tokens or settings.llm_max_tokens,
                system=system,
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise LLMError(self.name, self._describe(exc)) from exc
        except Exception as exc:
            raise LLMError(self.name, f"unexpected failure: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMError(self.name, "the model declined to answer this request")

        self._log_usage(response, kind="complete")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMError(self.name, "the model returned no text")
        return text

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _describe(self, exc: anthropic.APIError) -> str:
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

    def _log_usage(self, response: Any, kind: str) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:  # pragma: no cover - stubs in tests
            return
        logger.info(
            "llm_call",
            provider=self.name,
            kind=kind,
            model=getattr(response, "model", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
