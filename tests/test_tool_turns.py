"""Tool turns through the real OpenAI and Anthropic SDKs, over a mocked
transport that returns each vendor's documented streaming format.

The streams are SYNTHETIC (hand-built from the documented event shapes; the
APIs are not reachable where this was written). They prove the request each
SDK sends (tools, caching marks, transcript conversion) and that streamed
text, fragmented tool arguments and usage are assembled and metered.
"""

from __future__ import annotations

import json

import anthropic
import httpx
import httpx2
import openai
import sqlalchemy as sa

from app.models.usage import UsageEvent
from app.services.llm.anthropic import AnthropicProvider
from app.services.llm.base import ToolCall, ToolSpec
from app.services.llm.openai import OpenAIProvider

TOOLS = [
    ToolSpec("get_movements", "List a ticker's movements.", {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}),
    ToolSpec("compare", "Compare tickers.", {"type": "object", "properties": {"symbols": {"type": "array", "items": {"type": "string"}}}}),
]
TRANSCRIPT = [
    {"role": "user", "content": "Why did NVDA fall?"},
    {"role": "assistant", "content": "Looking.", "tool_calls": [
        ToolCall("call_1", "get_movements", {"symbol": "NVDA"}), ToolCall("call_2", "compare", {"symbols": ["NVDA", "AMD"]})]},
    {"role": "tool", "tool_call_id": "call_1", "name": "get_movements", "content": "[]"},
    {"role": "tool", "tool_call_id": "call_2", "name": "compare", "content": "{}"},
]


def sse(events: list[tuple[str | None, dict | str]]) -> bytes:
    lines = []
    for name, data in events:
        if name:
            lines.append(f"event: {name}")
        lines.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def recorder(body: bytes, seen: list[dict], http=httpx):
    """A client on `http` (httpx, or httpx2 for the Anthropic SDK) that records
    each request body and answers with `body` as an event stream."""

    def handler(request):
        seen.append(json.loads(request.content))
        return http.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return http.AsyncClient(transport=http.MockTransport(handler))


def openai_chunk(delta: dict, *, usage: dict | None = None, finish: str | None = None) -> dict:
    chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-test",
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if delta is not None else []}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


async def test_openai_streams_text_and_joins_tool_call_fragments(ledger):
    body = sse([
        (None, openai_chunk({"role": "assistant", "content": "Checking "})),
        (None, openai_chunk({"content": "the data."})),
        (None, openai_chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                             "function": {"name": "get_movements", "arguments": '{"sym'}}]})),
        (None, openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'bol": "NVDA"}'}}]})),
        (None, openai_chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                                             "function": {"name": "compare", "arguments": "not json"}}]}, finish="tool_calls")),
        (None, openai_chunk(None, usage={"prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230,
                                         "prompt_tokens_details": {"cached_tokens": 1024}})),
        (None, "[DONE]"),
    ])
    seen: list[dict] = []
    client = openai.AsyncOpenAI(api_key="sk-test", http_client=recorder(body, seen), max_retries=0)
    streamed: list[str] = []

    async def on_text(text: str) -> None:
        streamed.append(text)

    turn = await OpenAIProvider(model="gpt-4o-mini", client=client).tool_turn(
        system="SYSTEM", messages=TRANSCRIPT, tools=TOOLS, operation="chat", on_text=on_text
    )

    assert streamed == ["Checking ", "the data."] and turn.text == "Checking the data."
    first, second = turn.tool_calls
    assert (first.id, first.name, first.arguments, first.error) == ("call_a", "get_movements", {"symbol": "NVDA"}, None)
    assert second.name == "compare" and second.error.startswith("invalid arguments")
    assert (turn.input_tokens, turn.output_tokens) == (1200, 30)

    sent = seen[0]
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert [t["function"]["name"] for t in sent["tools"]] == ["get_movements", "compare"]
    assert sent["messages"][0] == {"role": "system", "content": "SYSTEM"}  # the stable prefix first
    assistant = sent["messages"][2]
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_1", "call_2"]
    assert json.loads(assistant["tool_calls"][1]["function"]["arguments"]) == {"symbols": ["NVDA", "AMD"]}
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "[]"}

    async with ledger() as session:
        event = await session.scalar(sa.select(UsageEvent))
    assert (event.operation, event.input_tokens, event.cache_read_tokens, event.output_tokens) == ("chat", 176, 1024, 30)


async def test_openai_without_tools_sends_none(ledger):
    body = sse([(None, openai_chunk({"content": "Answer [M1]."}, finish="stop")),
                (None, openai_chunk(None, usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13})),
                (None, "[DONE]")])
    seen: list[dict] = []
    client = openai.AsyncOpenAI(api_key="sk-test", http_client=recorder(body, seen), max_retries=0)
    turn = await OpenAIProvider(model="gpt-4o-mini", client=client).tool_turn(
        system="S", messages=[{"role": "user", "content": "q"}], tools=[], operation="chat"
    )
    assert turn.text == "Answer [M1]." and turn.tool_calls == []
    assert "tools" not in seen[0]


async def test_anthropic_streams_text_tool_input_and_caches_the_prefix(ledger):
    body = sse([
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 50, "output_tokens": 1, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me look."}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "get_movements", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"symbol":'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ' "NVDA"}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 42}}),
        ("message_stop", {"type": "message_stop"}),
    ])
    seen: list[dict] = []
    client = anthropic.AsyncAnthropic(api_key="sk-ant-test", http_client=recorder(body, seen, httpx2), max_retries=0)
    streamed: list[str] = []

    async def on_text(text: str) -> None:
        streamed.append(text)

    turn = await AnthropicProvider(model="claude-test-model", client=client).tool_turn(
        system="SYSTEM", messages=TRANSCRIPT, tools=TOOLS, operation="chat", on_text=on_text
    )

    assert streamed == ["Let me look."]
    (call,) = turn.tool_calls
    assert (call.id, call.name, call.arguments) == ("toolu_1", "get_movements", {"symbol": "NVDA"})
    assert (turn.input_tokens, turn.output_tokens) == (2050, 42)

    sent = seen[0]
    assert sent["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in sent["tools"][0]
    assert sent["tools"][0]["input_schema"]["required"] == ["symbol"]
    assistant, results = sent["messages"][1], sent["messages"][2]
    assert assistant["content"][0] == {"type": "text", "text": "Looking."}
    assert [b["id"] for b in assistant["content"][1:]] == ["call_1", "call_2"]
    assert results["role"] == "user" and [b["tool_use_id"] for b in results["content"]] == ["call_1", "call_2"]
    assert len(sent["messages"]) == 3  # two tool results, one user message

    async with ledger() as session:
        event = await session.scalar(sa.select(UsageEvent))
    assert (event.input_tokens, event.cache_read_tokens, event.output_tokens) == (50, 2000, 42)


async def test_a_provider_without_tool_support_says_so():
    import pytest

    from app.core.errors import LLMError
    from app.services.llm.base import LLMProvider

    class Plain(LLMProvider):
        available = True

        async def parse_structured(self, **kw):  # pragma: no cover
            raise NotImplementedError

        async def complete(self, **kw):  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(LLMError):
        await Plain().tool_turn(system="s", messages=[], tools=[], operation="chat")
