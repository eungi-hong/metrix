"""A stand-in for OpenAI's chat completions API, for load tests only.

Answers POST /v1/chat/completions after a fixed delay (FAKE_LLM_DELAY_SECONDS,
default 1.0, per call). With tools offered and no tool result yet, it calls
get_movements for the ticker in the question's "TICKERS:" line; otherwise it
answers citing the first movement and article labels the tools returned. It
streams when asked to (as Ask Metrix asks), so the whole path runs: tool
loop, citation checks, persistence, streaming. What it measures is Metrix's
own cost around a model of known latency, not a model's.

    uvicorn loadtest.fake_openai:app --port 8099
    OPENAI_BASE_URL=http://127.0.0.1:8099/v1 OPENAI_API_KEY=fake ...  # the API
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

DELAY = float(os.environ.get("FAKE_LLM_DELAY_SECONDS", "1.0"))
ANSWER = "The move followed the company's own news on that day [M1] [A1]."


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    if scope["method"] != "POST" or not scope["path"].endswith("/chat/completions"):
        await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error": "not found"}'})
        return
    request = json.loads(body or b"{}")
    await asyncio.sleep(DELAY)
    messages = request.get("messages", [])
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    results = "".join(str(m.get("content")) for m in messages[last_user + 1:] if m.get("role") == "tool")
    symbol = re.search(r"TICKERS: ([A-Z][A-Z.-]*)", str(messages[last_user].get("content", ""))) if messages else None
    call = None
    if request.get("tools") and not results and symbol:
        call = {"id": "call_fake", "type": "function",
                "function": {"name": "get_movements", "arguments": json.dumps({"symbol": symbol.group(1), "sort": "abs_return"})}}
        text = ""
    else:
        labels = re.findall(r'"label":"([MA]\d+)"', results)
        m = next((label for label in labels if label.startswith("M")), None)
        a = next((label for label in labels if label.startswith("A")), None)
        text = (f"The largest move was on the day of the company's own news [{m}]"
                + (f" [{a}]." if a else ".")) if m else ANSWER
    usage = {"prompt_tokens": 1500, "completion_tokens": 40, "total_tokens": 1540}
    created = int(time.time())
    if request.get("stream"):
        chunks = []
        if call:
            chunks.append({"tool_calls": [{"index": 0, **call}]})
        else:
            words = text.split(" ")
            chunks += [{"content": w + (" " if i < len(words) - 1 else "")} for i, w in enumerate(words)]
        frames = [
            {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": created, "model": request.get("model", "fake"),
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            for delta in chunks
        ]
        frames.append({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": created,
                       "model": request.get("model", "fake"), "choices": [], "usage": usage})
        payload = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": payload.encode()})
        return
    message = {"role": "assistant", "content": text or None}
    if call:
        message["tool_calls"] = [call]
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": created,
        "model": request.get("model", "fake"),
        "choices": [{"index": 0, "finish_reason": "tool_calls" if call else "stop", "message": message}],
        "usage": usage,
    }
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode()})
