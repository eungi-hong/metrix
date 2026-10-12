"""Public Exa/OpenAI key handling, without ever exercising a real key."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import openai
import pytest
import sqlalchemy as sa
from pydantic import BaseModel

from app.api.deps import (
    provider_credentials,
    request_llm,
    request_news_provider,
)
from app.core.config import LLMPrice, settings
from app.core.errors import ConfigurationError
from app.services import ratelimit
from app.services.news.exa import ExaNewsProvider
from app.services.news.fixture import FixtureNewsProvider
from app.services.llm.openai import OpenAIProvider
from app.models.usage import UsageEvent
from app.models.enums import NewsStatus
from app.models.market import Movement
from tests.conftest import StubLLM, all_jobs, api_app, api_client
from tests.test_spend import exa_client, search_request


async def test_provider_key_headers_must_be_a_complete_pair():
    credentials = await provider_credentials("exa_test", "sk-test")
    assert credentials.supplied_by_user
    assert credentials.exa_api_key == "exa_test"
    assert credentials.openai_api_key == "sk-test"

    with pytest.raises(ConfigurationError, match="Provide both"):
        await provider_credentials("exa_test", None)


async def test_browser_keys_force_inline_work_and_never_create_a_job(
    session_factory, monkeypatch
):
    """A user key can never be copied into a background-job payload."""
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setitem(
        settings.plan_limits_json,
        "anonymous",
        settings.plan_limits_json["anonymous"].model_copy(update={"allow_wait": False}),
    )
    app = api_app(session_factory, StubLLM())

    async def stub_llm():
        yield StubLLM()

    async def fixture_news():
        yield FixtureNewsProvider()

    app.dependency_overrides[request_llm] = stub_llm
    app.dependency_overrides[request_news_provider] = fixture_news
    headers = {"X-Exa-Api-Key": "exa_test", "X-OpenAI-Api-Key": "sk-test"}

    async with api_client(app) as client:
        response = await client.get("/tickers/TEST", params={"wait": True}, headers=headers)
        assert response.status_code == 200

        # An open news window would normally queue a follow-up. In BYOK mode
        # it must stay request-only: no later worker can use or need the key.
        async with session_factory() as session:
            movement = await session.scalar(sa.select(Movement))
            assert movement is not None
            movement.news_status = NewsStatus.PENDING
            movement.news_fetched_at = None
            movement.news_window_closes_at = datetime.now(timezone.utc) + timedelta(days=1)
            await session.commit()
        response = await client.get("/tickers/TEST", params={"wait": True}, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert await all_jobs(session_factory) == []


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [("anthropic", "claude-sonnet-4-6", "byok-model"), ("openai", "gpt-test", "gpt-test")],
)
async def test_browser_openai_key_never_borrows_another_vendors_model(
    monkeypatch, provider, model, expected
):
    """An Anthropic deployment's LLM_MODEL would be a 404 on OpenAI."""
    monkeypatch.setattr(settings, "llm_provider", provider)
    monkeypatch.setattr(settings, "llm_model", model)
    monkeypatch.setattr(settings, "byok_openai_model", "byok-model")
    credentials = await provider_credentials(x_exa_api_key="exa_test", x_openai_api_key="sk-test")

    llm = request_llm(credentials, StubLLM())
    client = await anext(llm)
    await llm.aclose()

    assert isinstance(client, OpenAIProvider)
    assert client.model == expected


class _StructuredResult(BaseModel):
    answer: str


class _OpenAICompletions:
    async def parse(self, **kwargs):
        return type(
            "Response",
            (),
            {
                "model": kwargs["model"],
                "usage": type(
                    "Usage",
                    (),
                    {
                        "prompt_tokens": 100,
                        "completion_tokens": 25,
                        "prompt_tokens_details": type("Details", (), {"cached_tokens": 10})(),
                    },
                )(),
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"parsed": _StructuredResult(answer="ok")})()},
                    )()
                ],
            },
        )()


async def test_openai_seam_uses_structured_output_and_records_usage(ledger, monkeypatch):
    """The new provider stays behind the same metered LLM seam as the old one."""
    monkeypatch.setattr(
        settings,
        "llm_prices_json",
        {
            "test-model": LLMPrice(
                input_per_mtok=1,
                output_per_mtok=2,
                cache_read_per_mtok=0.5,
                cache_write_per_mtok=1,
            )
        },
    )
    completions = _OpenAICompletions()
    client = type(
        "Client",
        (),
        {"beta": type("Beta", (), {"chat": type("Chat", (), {"completions": completions})()})()},
    )()
    provider = OpenAIProvider(model="test-model", client=client)

    result = await provider.parse_structured(
        system="Return the answer.",
        user="Is this wired?",
        output_model=_StructuredResult,
        operation="test",
    )

    assert result.answer == "ok"
    async with ledger() as session:
        event = await session.scalar(sa.select(UsageEvent))
    assert event is not None
    assert (event.provider, event.input_tokens, event.output_tokens, event.cache_read_tokens) == (
        "openai", 90, 25, 10,
    )


@pytest.mark.parametrize(
    ("code", "blocked", "message"),
    [("insufficient_quota", False, "out of credit"), ("rate_limit_exceeded", True, "rate limited")],
)
async def test_an_empty_openai_balance_is_not_a_rate_limit(monkeypatch, code, blocked, message):
    """Both arrive as 429; only the real rate limit should pause other callers."""
    backoffs: list[float] = []
    monkeypatch.setattr(
        ratelimit, "bucket", lambda name: type("Limiter", (), {"backoff": lambda self, s: backoffs.append(s)})()
    )
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    exc = openai.RateLimitError(
        "429", response=httpx.Response(429, request=request), body={"code": code}
    )

    error = OpenAIProvider(api_key="sk-test")._error(exc)

    assert message in str(error)
    assert bool(backoffs) is blocked


async def test_browser_keys_are_not_held_to_the_anonymous_plan(session_factory, monkeypatch):
    """The caller pays for the work, so production's zero anonymous plan does not apply."""
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setitem(
        settings.plan_limits_json,
        "anonymous",
        settings.plan_limits_json["anonymous"].model_copy(
            update={"cold_ingests_per_day": 0, "refresh_per_day": 0, "chat_per_minute": 0, "chat_per_day": 0}
        ),
    )
    app = api_app(session_factory, StubLLM())

    async def stub_llm():
        yield StubLLM()

    async def fixture_news():
        yield FixtureNewsProvider()

    app.dependency_overrides[request_llm] = stub_llm
    app.dependency_overrides[request_news_provider] = fixture_news
    headers = {"X-Exa-Api-Key": "exa_test", "X-OpenAI-Api-Key": "sk-test"}

    async with api_client(app) as client:
        keyless = await client.get("/tickers/OTHER")
        cold = await client.get("/tickers/TEST", params={"wait": True}, headers=headers)
        chat = await client.post("/chat", json={"question": "Why did it move?"}, headers=headers)
        keyless_chat = await client.post("/chat", json={"question": "Why did it move?"})

    assert keyless.status_code == 429
    assert cold.status_code == 200 and cold.json()["status"] == "ready"
    assert chat.status_code == 200
    assert keyless_chat.status_code == 429


async def test_a_call_on_the_callers_key_is_neither_capped_nor_recorded(ledger, monkeypatch):
    monkeypatch.setattr(settings, "daily_spend_cap_usd", 0.0)
    calls: list[httpx.Request] = []
    provider = ExaNewsProvider(
        api_key="exa_test",
        client=exa_client({"results": [], "costDollars": {"total": 0.01}}, calls),
        caller_pays=True,
    )

    await provider.execute(search_request())

    assert len(calls) == 1
    async with ledger() as session:
        assert await session.scalar(sa.select(UsageEvent)) is None
