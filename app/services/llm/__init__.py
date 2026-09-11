"""LLM providers."""

from __future__ import annotations

from app.core.config import settings
from app.services.llm.anthropic import AnthropicProvider
from app.services.llm.base import LLMProvider, MessageParam

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
}


def build_llm_client(provider_name: str | None = None) -> LLMProvider:
    """The configured provider."""
    name = (provider_name or settings.llm_provider).lower()
    try:
        provider_cls = _PROVIDERS[name]
    except KeyError:  # pragma: no cover - guarded by pydantic Literal
        raise ValueError(f"Unknown LLM provider '{name}'") from None
    return provider_cls()


_default_client: LLMProvider | None = None


def get_llm_client() -> LLMProvider:
    """Process-wide client. Overridden in tests via FastAPI dependency_overrides."""
    global _default_client
    if _default_client is None:
        _default_client = build_llm_client()
    return _default_client


__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "MessageParam",
    "build_llm_client",
    "get_llm_client",
]
