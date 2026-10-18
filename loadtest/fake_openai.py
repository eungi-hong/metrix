"""A stand-in for OpenAI's chat completions API, for load tests only.

Answers POST /v1/chat/completions after a fixed delay (FAKE_LLM_DELAY_SECONDS,
default 1.0) with a short answer that cites the first movement, so Ask Metrix
runs its whole path (history, context, the call, citation handling,
persistence) without a key or spend. What it measures is Metrix's own cost
around a model of known latency, not a model's.

    uvicorn loadtest.fake_openai:app --port 8099
    OPENAI_BASE_URL=http://127.0.0.1:8099/v1 OPENAI_API_KEY=fake ...  # the API
"""

from __future__ import annotations

import asyncio
import json
import os
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
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.get("model", "fake"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": ANSWER}}],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 40, "total_tokens": 1540},
    }
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode()})
