"""Shared FastAPI dependencies.

The LLM client is a dependency rather than a module-level import so tests can
substitute a stub through `app.dependency_overrides` without patching.

`CurrentUser` is the one place a request's identity is decided. Today that is
an API key; a move to JWT or OAuth would change this function and nothing
that depends on it.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.context import current_user_id
from app.core.errors import ConfigurationError
from app.db.session import get_session
from app.services import auth
from app.services.auth import Principal
from app.services.llm import LLMProvider, get_llm_client

SessionDep = Annotated[AsyncSession, Depends(get_session)]
LLMDep = Annotated[LLMProvider, Depends(get_llm_client)]

# auto_error=False: a missing header is decided below (anonymous or 401),
# not by FastAPI's own 403. Also what puts the key field in /docs.
_bearer = HTTPBearer(auto_error=False, description="An API key: mtx_...")


async def current_principal(
    request: Request,
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
