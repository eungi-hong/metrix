"""POST /chat/stream: the same turn as POST /chat, as server-sent events."""

from __future__ import annotations

import json

from app.core.errors import LLMError
from tests.conftest import StubLLM, api_app, api_client
from tests.test_finance_api import ready  # noqa: F401  (fixture)


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


async def test_the_stream_shows_lookups_text_and_the_checked_answer(client, stub_llm, ready):  # noqa: F811
    response = await client.post("/chat/stream", json={"question": "Why did SYNTH fall?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    received = events(response.text)
    kinds = [kind for kind, _ in received]
    assert kinds == ["tool_call", "tool_result", "delta", "delta", "done"]
    assert received[0][1] == {"name": "get_movements", "arguments": {"symbol": "SYNTH", "sort": "abs_return"}}
    assert received[1][1]["rows"] > 0
    done = received[-1][1]
    assert "".join(d["text"] for k, d in received if k == "delta") == done["answer"]
    assert done["grounded"] and done["answered_by"] == "model" and done["sources"]["movements"]

    follow_up = await client.post("/chat", json={"question": "And then?", "conversation_id": done["conversation_id"]})
    assert follow_up.status_code == 200  # the streamed turn was persisted


async def test_an_unknown_conversation_is_a_404_before_streaming(client):
    response = await client.post("/chat/stream", json={"question": "Q?", "conversation_id": "not-mine"})
    assert response.status_code == 404
    assert (await client.post("/chat/stream", json={"question": "  "})).status_code == 422


async def test_a_failing_model_streams_the_fallback(session_factory, user_key, ready):  # noqa: F811
    class Down(StubLLM):
        async def tool_turn(self, **kwargs):
            raise LLMError("openai", "HTTP 500")

    async with api_client(api_app(session_factory, Down()), user_key[1]) as client:
        received = events((await client.post("/chat/stream", json={"question": "Why did SYNTH move?"})).text)
    assert [k for k, _ in received] == ["done"]
    assert received[0][1]["answered_by"] == "fallback" and received[0][1]["grounded"]
    assert received[0][1]["fallback_reason"] == "unavailable"


async def test_an_empty_provider_balance_is_reported_as_out_of_credit(session_factory, user_key, ready):  # noqa: F811
    class Broke(StubLLM):
        async def tool_turn(self, **kwargs):
            raise LLMError("openai", "the OpenAI account is out of credit", permanent=True, out_of_credit=True)

    async with api_client(api_app(session_factory, Broke()), user_key[1]) as client:
        body = (await client.post("/chat", json={"question": "Why did SYNTH move?"})).json()
    assert body["answered_by"] == "fallback" and body["fallback_reason"] == "out_of_credit"
    assert "out of credit" not in body["answer"]  # the provider's own words stay in the log


async def test_an_internal_failure_is_an_error_event(session_factory, user_key, ready):  # noqa: F811
    class Broken(StubLLM):
        async def tool_turn(self, **kwargs):
            raise RuntimeError("bug")

    async with api_client(api_app(session_factory, Broken()), user_key[1]) as client:
        received = events((await client.post("/chat/stream", json={"question": "SYNTH?"})).text)
    assert received == [("error", {"error": "internal_error", "detail": "The answer failed."})]
