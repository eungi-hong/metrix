"""Shared FastAPI dependencies.

The LLM client is a dependency rather than a module-level import so tests can
substitute a stub through `app.dependency_overrides` without patching.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.llm import LLMClient, get_llm_client

SessionDep = Annotated[AsyncSession, Depends(get_session)]
LLMDep = Annotated[LLMClient, Depends(get_llm_client)]
