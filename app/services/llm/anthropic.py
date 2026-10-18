"""Anthropic implementation of the LLM seam.

The only module in the application that imports the Anthropic SDK. Everything
vendor-shaped -- client construction, SDK error classes, response block
walking -- is contained here; callers see `LLMProvider` and `LLMError`.

Every call is metered here (`app.services.spend`): reserved against the daily
cap before it is made, and recorded in the ledger once the response arrives,
before anything else can fail, since a response is billed whether or not it
turns out to be usable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic

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


class AnthropicProvider(LLMProvider):
    """Async Anthropic client with uniform error handling."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        caller_pays: bool = False,
    ) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.llm_model
        self._client = client
        # The key is the caller's own: their spend, not ours (see spend.metered).
        self._caller_pays = caller_pays

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
            await ratelimit.bucket("anthropic").acquire()
            try:
                response = await client.messages.parse(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_format=output_model,
                )
            except anthropic.APIError as exc:
                raise self._error(exc) from exc
            except Exception as exc:
                raise LLMError(self.name, f"unexpected failure: {exc}") from exc
            await self._record(meter, response, kind="structured")

        parsed = response.parsed_output
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
            await ratelimit.bucket("anthropic").acquire()
            try:
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                raise self._error(exc) from exc
            except Exception as exc:
                raise LLMError(self.name, f"unexpected failure: {exc}") from exc
            await self._record(meter, response, kind="complete")

        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMError(self.name, "the model declined to answer this request")

        text = "".join(b.text for b in response.content if b.type == "text").strip()
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
        """Streamed Messages API call with tools.

        The system prompt and the last tool definition carry `cache_control`,
        so the stable prefix (instructions and tool schemas, identical on every
        turn) is cached by the provider. Text deltas go to `on_text`; tool
        inputs arrive as JSON fragments per content block and are joined here.
        """
        client = self._require_client()
        max_tokens = max_tokens or settings.llm_max_tokens
        wire_tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        if wire_tools:
            wire_tools[-1] = {**wire_tools[-1], "cache_control": {"type": "ephemeral"}}
        wire_messages = _anthropic_messages(messages)
        prompt_chars = len(system) + len(json.dumps(wire_tools)) + len(json.dumps(wire_messages, default=str))
        estimate = spend.llm_estimate(self.model, prompt_chars=prompt_chars, max_tokens=max_tokens)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": wire_messages,
            "stream": True,
        }
        if wire_tools:
            request["tools"] = wire_tools
        text_parts: list[str] = []
        blocks: dict[int, dict[str, str]] = {}
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        stop_reason = None
        async with spend.metered(self.name, operation, estimate, caller_pays=self._caller_pays) as meter:
            await ratelimit.bucket("anthropic").acquire()
            try:
                stream = await client.messages.create(**request)
                async for event in stream:
                    kind = event.type
                    if kind == "message_start":
                        start_usage = getattr(event.message, "usage", None)
                        for name in usage:
                            value = getattr(start_usage, name, None)
                            if isinstance(value, int):
                                usage[name] = value
                    elif kind == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            blocks[event.index] = {"id": block.id, "name": block.name, "json": ""}
                    elif kind == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            text_parts.append(delta.text)
                            if on_text is not None:
                                await on_text(delta.text)
                        elif delta.type == "input_json_delta" and event.index in blocks:
                            blocks[event.index]["json"] += delta.partial_json
                    elif kind == "message_delta":
                        output = getattr(getattr(event, "usage", None), "output_tokens", None)
                        if isinstance(output, int):
                            usage["output_tokens"] = output
                        stop_reason = getattr(event.delta, "stop_reason", None) or stop_reason
            except anthropic.APIError as exc:
                raise self._error(exc) from exc
            except Exception as exc:
                raise LLMError(self.name, f"unexpected failure: {exc}") from exc
            await self._record(meter, SimpleNamespace(usage=SimpleNamespace(**usage), model=self.model), kind="tool_turn")

        if stop_reason == "refusal":
            raise LLMError(self.name, "the model declined to answer this request")
        return ToolTurn(
            text="".join(text_parts),
            tool_calls=[_tool_call(blocks[i]) for i in sorted(blocks)],
            input_tokens=usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
            output_tokens=usage["output_tokens"],
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _error(self, exc: anthropic.APIError) -> LLMError:
        if isinstance(exc, anthropic.RateLimitError):
            # The SDK has already retried; slow every call in this process.
            ratelimit.bucket("anthropic").backoff(
                ratelimit.retry_after_seconds(exc.response.headers.get("retry-after"))
            )
        # A rejected key fails the same way on every retry.
        permanent = isinstance(
            exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
        )
        return LLMError(self.name, self._describe(exc), permanent=permanent)

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

    async def _record(self, meter: spend.Reservation, response: Any, kind: str) -> None:
        """Log the call's usage and settle its cost in the ledger.

        Priced by `self.model`, the configured id the price table is keyed
        by, not the dated id the response may echo back.
        """
        usage = getattr(response, "usage", None)
        tokens = {
            "input_tokens": _tokens(usage, "input_tokens"),
            "output_tokens": _tokens(usage, "output_tokens"),
            "cache_read_tokens": _tokens(usage, "cache_read_input_tokens"),
            "cache_write_tokens": _tokens(usage, "cache_creation_input_tokens"),
        }
        logger.info(
            "llm_call",
            provider=self.name,
            kind=kind,
            model=getattr(response, "model", None),
            **tokens,
        )
        if usage is None:
            # No usage to price: record the reservation's estimate, flagged.
            await meter.settle(cost_usd=meter.estimate, estimated=True, model=self.model)
            return
        cost, estimated = spend.llm_cost(self.model, **tokens)
        await meter.settle(cost_usd=cost, estimated=estimated, model=self.model, **tokens)


def _tokens(usage: Any, name: str) -> int:
    value = getattr(usage, name, None)
    return value if isinstance(value, int) else 0


def _tool_call(block: dict[str, str]) -> ToolCall:
    try:
        arguments = json.loads(block["json"] or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("input must be an object")
        return ToolCall(id=block["id"], name=block["name"], arguments=arguments)
    except ValueError as exc:
        return ToolCall(id=block["id"], name=block["name"], arguments={}, error=f"invalid arguments: {exc}")


def _anthropic_messages(messages: list[MessageParam]) -> list[dict[str, Any]]:
    """The neutral tool transcript (see llm.base) in Anthropic's wire shape.

    Tool results go back as `tool_result` blocks in a user message; results
    for one assistant turn's several calls share a single user message.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and all(
                b.get("type") == "tool_result" for b in out[-1]["content"]
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant" and message.get("tool_calls"):
            content: list[dict[str, Any]] = []
            if message.get("content"):
                content.append({"type": "text", "text": message["content"]})
            content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in message["tool_calls"]]
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": role, "content": message["content"]})
    return out
