"""Shared FastAPI dependencies.

The LLM client is a dependency rather than a module-level import so tests can
substitute a stub through `app.dependency_overrides` without patching.

`CurrentUser` is the one place a request's identity is decided. Today that is
an API key; a move to JWT or OAuth would change this function and nothing
that depends on it. It also charges the caller's `requests_per_minute`, so
every authenticated call counts, and puts that quota's X-RateLimit-* headers
on the response.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.context import current_user_id
from app.core.errors import ConfigurationError
from app.db.session import get_session
from app.services import auth, quotas
from app.services.auth import Principal
from app.services.llm import LLMProvider, build_llm_client, get_llm_client
from app.services.news import NewsProvider, build_news_provider

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# auto_error=False: a missing header is decided below (anonymous or 401),
# not by FastAPI's own 403. Also what puts the key field in /docs.
_bearer = HTTPBearer(auto_error=False, description="An API key: mtx_...")


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    """Request-scoped credentials for the public browser's BYOK mode.

    A complete pair switches both provider seams for that request. The values
    only live in the dependency and provider clients: they are never added to
    a model, a queued job, an error response, or a log event.
    """

    exa_api_key: str | None = field(repr=False)
    openai_api_key: str | None = field(repr=False)

    @property
    def supplied_by_user(self) -> bool:
        return self.exa_api_key is not None


def _clean_key(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


async def provider_credentials(
    x_exa_api_key: Annotated[str | None, Header(alias="X-Exa-Api-Key")] = None,
    x_openai_api_key: Annotated[str | None, Header(alias="X-OpenAI-Api-Key")] = None,
) -> ProviderCredentials:
    """Read a paired provider key override, refusing partial configuration."""
    exa_key = _clean_key(x_exa_api_key)
    openai_key = _clean_key(x_openai_api_key)
    if bool(exa_key) != bool(openai_key):
        raise ConfigurationError(
            "Provide both X-Exa-Api-Key and X-OpenAI-Api-Key, or neither to use demo access."
        )
    return ProviderCredentials(exa_api_key=exa_key, openai_api_key=openai_key)


ProviderCredentialsDep = Annotated[ProviderCredentials, Depends(provider_credentials)]


async def request_llm(
    credentials: ProviderCredentialsDep,
    configured_client: Annotated[LLMProvider, Depends(get_llm_client)],
) -> AsyncIterator[LLMProvider]:
    """Use a browser's OpenAI key only for the request that sent it."""
    if not credentials.supplied_by_user:
        yield configured_client
        return
    model = settings.llm_model if settings.llm_provider == "openai" else settings.byok_openai_model
    client = build_llm_client("openai", api_key=credentials.openai_api_key, model=model)
    try:
        yield client
    finally:
        await client.aclose()


async def request_news_provider(
    session: SessionDep, credentials: ProviderCredentialsDep
) -> AsyncIterator[NewsProvider]:
    """Use a browser's Exa key only for inline work in this request."""
    provider = build_news_provider(session, api_key=credentials.exa_api_key)
    try:
        yield provider
    finally:
        await provider.aclose()


LLMDep = Annotated[LLMProvider, Depends(request_llm)]
NewsDep = Annotated[NewsProvider, Depends(request_news_provider)]


async def current_principal(
    request: Request,
    response: Response,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Who is calling. 401 without a valid key, unless AUTH_REQUIRED is off and
    no key was sent, in which case the caller is anonymous, keyed by IP.

    Also sets `current_user_id`, so spend this request causes is attributed.
    """
    ip = auth.client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
    )
    if credentials is None:
        if settings.auth_required:
            auth.refuse_missing(ip)
        principal = auth.anonymous(ip)
    else:
        principal = await auth.authenticate(session, credentials.credentials, ip)
    current_user_id.set(principal.user_id)
    allowed = await quotas.charge(principal, quotas.Quota.REQUESTS_PER_MINUTE)
    response.headers.update(allowed.headers())
    return principal


CurrentUser = Annotated[Principal, Depends(current_principal)]


def admin_token_valid(token: str | None) -> bool:
    """Whether `token` is the configured admin token. Raises 503 if none is set."""
    if not settings.admin_token:
        raise ConfigurationError("ADMIN_TOKEN is not set; the admin endpoints are disabled.")
    return token is not None and secrets.compare_digest(token, settings.admin_token)


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    if not admin_token_valid(x_admin_token):
        raise HTTPException(status_code=401, detail="Missing or wrong X-Admin-Token.")


async def admin_view(x_admin_token: Annotated[str | None, Header()] = None) -> bool:
    """For endpoints users and admins share: True when a valid X-Admin-Token is
    sent. A wrong one is a 401, not a silent fall-back to the user's view."""
    if x_admin_token is None:
        return False
    require_admin(x_admin_token)
    return True


AdminView = Annotated[bool, Depends(admin_view)]
