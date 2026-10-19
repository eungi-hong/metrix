"""Why chat fell back, as one of a few categories a visitor can act on."""

from __future__ import annotations

from datetime import UTC, datetime

import anthropic
import httpx
import openai
import pytest

from app.core.errors import (
    ConfigurationError,
    LLMError,
    ProviderBusy,
    SpendCapReached,
)
from app.services.chat import fallback_reason
from app.services.llm.anthropic import AnthropicProvider
from app.services.llm.openai import OpenAIProvider


def test_each_failure_maps_to_its_category():
    soon = datetime(2026, 10, 20, tzinfo=UTC)
    assert fallback_reason(LLMError("openai", "x", permanent=True, out_of_credit=True)) == "out_of_credit"
    assert fallback_reason(SpendCapReached("chat", soon, "cap")) == "spend_cap"
    assert fallback_reason(ConfigurationError("no key")) == "not_configured"
    assert fallback_reason(LLMError("openai", "authentication failed", permanent=True)) == "not_configured"
    assert fallback_reason(LLMError("openai", "HTTP 500")) == "unavailable"


def test_a_provider_that_is_rate_limiting_is_busy():
    soon = datetime(2026, 10, 20, tzinfo=UTC)
    assert fallback_reason(ProviderBusy("openai", soon, "rate limited")) == "busy"


def _openai_error(code: str) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"code": code}})
    return openai.RateLimitError("429", response=response, body={"code": code})


@pytest.mark.parametrize("code", ["insufficient_quota", "credit_balance_exhausted"])
def test_openai_marks_an_empty_balance_out_of_credit(code):
    error = OpenAIProvider(api_key="sk-test")._error(_openai_error(code))
    assert error.out_of_credit and error.permanent


def test_openai_rate_limits_are_not_out_of_credit():
    error = OpenAIProvider(api_key="sk-test")._error(_openai_error("rate_limit_exceeded"))
    assert not error.out_of_credit


def test_anthropic_marks_a_low_credit_balance_out_of_credit():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    message = "Your credit balance is too low to access the Anthropic API."
    response = httpx.Response(400, request=request, json={"error": {"message": message}})
    exc = anthropic.BadRequestError(message, response=response, body={"error": {"message": message}})
    error = AnthropicProvider(api_key="sk-ant-test")._error(exc)
    assert error.out_of_credit and error.permanent
    assert "out of credit" in str(error)
