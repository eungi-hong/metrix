"""OpenAI implementation of the LLM seam.

This is the only module that knows the OpenAI SDK request and response shapes.
Keeping that detail here makes browser-supplied demo keys safe to use for one
request without leaking their vendor API into the ingestion or chat services.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import openai

from app.core.config import settings
from app.core.errors import ConfigurationError, LLMError
from app.core.logging import get_logger
from app.services import ratelimit, spend
from app.services.llm.base import (
    LLMProvider,
    MessageParam,
    T,
    TextCallback,
    ToolCall,
    ToolSpec,
    ToolTurn,
)

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """Async OpenAI client with structured output and uniformly safe errors."""

    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: openai.AsyncOpenAI | None = None,
        *,
        caller_pays: bool = False,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self.model = model or settings.llm_model
        self._client = client
        # The key is the caller's own: their spend, not ours (see spend.metered).
        self._caller_pays = caller_pays

    @property
    def available(self) -> bool:
        return bool(self._api_key) or self._client is not None

    def _require_client(self) -> openai.AsyncOpenAI:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is not set; relevance scoring and chat are unavailable."
            )
        self._client = openai.AsyncOpenAI(
            api_key=self._api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )
        return self._client

    async def parse_structured(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
        operation: str,
        max_tokens: int | None = None,
    ) -> T:
        client = self._require_client()
        max_tokens = max_tokens or settings.llm_max_tokens
        estimate = spend.llm_estimate(
            self.model, prompt_chars=len(system) + len(user), max_tokens=max_tokens
        )
        async with spend.metered(
            self.name, operation, estimate, caller_pays=self._caller_pays
        ) as meter:
            await ratelimit.bucket(self.name).acquire()
            try:
                response = await client.beta.chat.completions.parse(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format=output_model,
                )
            except openai.APIError as exc:
                raise self._error(exc) from exc
            except Exception as exc:
                raise LLMError(self.name, f"unexpected failure: {exc}") from exc
            await self._record(meter, response, kind="structured")

        choice = response.choices[0] if response.choices else None
        parsed = choice.message.parsed if choice is not None else None
        if parsed is None:
            raise LLMError(self.name, "structured output was empty or unparseable")
        return parsed

    async def complete(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        operation: str,
        max_tokens: int | None = None,
    ) -> str:
        client = self._require_client()
        max_tokens = max_tokens or settings.llm_max_tokens
        prompt_chars = len(system) + sum(len(str(m.get("content", ""))) for m in messages)
        estimate = spend.llm_estimate(self.model, prompt_chars=prompt_chars, max_tokens=max_tokens)
        async with spend.metered(
            self.name, operation, estimate, caller_pays=self._caller_pays
        ) as meter:
            await ratelimit.bucket(self.name).acquire()
            try:
                response = await client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}, *messages],
                )
            except openai.APIError as exc:
                raise self._error(exc) from exc
            except Exception as exc:
                raise LLMError(self.name, f"unexpected failure: {exc}") from exc
            await self._record(meter, response, kind="complete")

        choice = response.choices[0] if response.choices else None
        text = choice.message.content.strip() if choice and choice.message.content else ""
        if not text:
            raise LLMError(self.name, "the model returned no text")
        return text

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
        """Streamed chat completion with function tools.

        The system prompt and tool definitions come first and never change
        between turns, so OpenAI's automatic prompt caching applies to them.
        Tool-call fragments arrive by index and are joined here; usage comes
        in the final chunk (`include_usage`).
        """
        client = self._require_client()
        max_tokens = max_tokens or settings.llm_max_tokens
        wire = [{"role": "system", "content": system}, *_openai_messages(messages)]
        prompt_chars = sum(len(json.dumps(m, default=str)) for m in wire) + sum(
            len(json.dumps(t.parameters)) + len(t.description) for t in tools
        )
        estimate = spend.llm_estimate(self.model, prompt_chars=prompt_chars, max_tokens=max_tokens)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": wire,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = None
        async with spend.metered(self.name, operation, estimate, caller_pays=self._caller_pays) as meter:
            await ratelimit.bucket(self.name).acquire()
            try:
                stream = await client.chat.completions.create(**request)
                async for chunk in stream:
                    if getattr(chunk, "usage", None) is not None:
                        usage = chunk.usage
                    for choice in chunk.choices or []:
                        delta = choice.delta
                        if delta is None:
                            continue
                        if delta.content:
                            text_parts.append(delta.content)
                            if on_text is not None:
                                await on_text(delta.content)
                        for fragment in delta.tool_calls or []:
                            slot = calls.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                            if fragment.id:
                                slot["id"] = fragment.id
                            if fragment.function is not None:
                                slot["name"] += fragment.function.name or ""
                                slot["arguments"] += fragment.function.arguments or ""
            except openai.APIError as exc:
                raise self._error(exc) from exc
            except Exception as exc:
                raise LLMError(self.name, f"unexpected failure: {exc}") from exc
            await self._record(meter, SimpleNamespace(usage=usage, model=self.model), kind="tool_turn")

        return ToolTurn(
            text="".join(text_parts),
            tool_calls=[_tool_call(calls[i]) for i in sorted(calls)],
            input_tokens=_tokens(usage, "prompt_tokens"),
            output_tokens=_tokens(usage, "completion_tokens"),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _error(self, exc: openai.APIError) -> LLMError:
        if _out_of_quota(exc):
            # A 429, but about billing, not pace: waiting will not help, and
            # backing off would stall every other caller for nothing.
            return LLMError(self.name, self._describe(exc), permanent=True)
        if isinstance(exc, openai.RateLimitError):
            headers = exc.response.headers if exc.response is not None else {}
            # Which limit was hit, from OpenAI's own headers: nothing secret.
            logger.warning(
                "openai_rate_limited",
                code=exc.code,
                model=self.model,
                limit_requests=headers.get("x-ratelimit-limit-requests"),
                remaining_requests=headers.get("x-ratelimit-remaining-requests"),
                limit_tokens=headers.get("x-ratelimit-limit-tokens"),
                remaining_tokens=headers.get("x-ratelimit-remaining-tokens"),
                retry_after=headers.get("retry-after"),
            )
            ratelimit.bucket(self.name).backoff(
                ratelimit.retry_after_seconds(headers.get("retry-after"))
            )
        permanent = isinstance(
            exc, (openai.AuthenticationError, openai.PermissionDeniedError)
        )
        return LLMError(self.name, self._describe(exc), permanent=permanent)

    def _describe(self, exc: openai.APIError) -> str:
        """A message safe to surface, without leaking keys or full payloads."""
        if isinstance(exc, openai.AuthenticationError):
            return "authentication failed -- check OPENAI_API_KEY"
        if isinstance(exc, openai.PermissionDeniedError):
            return "permission denied -- check OPENAI_API_KEY permissions"
        if _out_of_quota(exc):
            return "the OpenAI account is out of credit -- check its billing"
        if isinstance(exc, openai.RateLimitError):
            return "rate limited"
        if isinstance(exc, openai.APIStatusError):
            return f"HTTP {exc.status_code}: {str(exc.message)[:200]}"
        if isinstance(exc, openai.APIConnectionError):
            return "could not reach the OpenAI API"
        return str(exc)[:200]

    async def _record(self, meter: spend.Reservation, response: Any, kind: str) -> None:
        usage = getattr(response, "usage", None)
        prompt_tokens = _tokens(usage, "prompt_tokens")
        cache_read_tokens = _tokens(getattr(usage, "prompt_tokens_details", None), "cached_tokens")
        tokens = {
            # OpenAI's prompt total includes cached input. Split it so the
            # configured cached-input price is applied only once.
            "input_tokens": max(prompt_tokens - cache_read_tokens, 0),
            "output_tokens": _tokens(usage, "completion_tokens"),
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": 0,
        }
        logger.info(
            "llm_call",
            provider=self.name,
            kind=kind,
            model=getattr(response, "model", None),
            **tokens,
        )
        if usage is None:
            await meter.settle(cost_usd=meter.estimate, estimated=True, model=self.model)
            return
        cost, estimated = spend.llm_cost(self.model, **tokens)
        await meter.settle(cost_usd=cost, estimated=estimated, model=self.model, **tokens)


def _tokens(usage: Any, name: str) -> int:
    value = getattr(usage, name, None)
    return value if isinstance(value, int) else 0


# Prepaid accounts that have spent their credit report the second code.
_OUT_OF_CREDIT_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted"})


def _out_of_quota(exc: openai.APIError) -> bool:
    """OpenAI answers 429 both for going too fast and for an empty balance."""
    return isinstance(exc, openai.RateLimitError) and exc.code in _OUT_OF_CREDIT_CODES


def _tool_call(slot: dict[str, str]) -> ToolCall:
    try:
        arguments = json.loads(slot["arguments"] or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        return ToolCall(id=slot["id"], name=slot["name"], arguments=arguments)
    except ValueError as exc:
        return ToolCall(id=slot["id"], name=slot["name"], arguments={}, error=f"invalid arguments: {exc}")


def _openai_messages(messages: list[MessageParam]) -> list[dict[str, Any]]:
    """The neutral tool transcript (see llm.base) in OpenAI's wire shape."""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"]})
        elif role == "assistant" and message.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in message["tool_calls"]
                    ],
                }
            )
        else:
            out.append({"role": role, "content": message["content"]})
    return out
