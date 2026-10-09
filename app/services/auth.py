"""API keys: issuing, checking, revoking; and who a request is from.

Keys are `mtx_` followed by 32 random bytes from `secrets.token_urlsafe`.
Only the SHA-256 of the whole key is stored. A fast hash is the right choice
here, unlike for passwords: a password is short and guessable, so its hash
must be slow to make guessing expensive, but these keys carry 256 bits of
randomness, which no amount of guessing gets through. A slow hash such as
bcrypt would add tens of milliseconds to every request and no security.

A key is found by its prefix, `mtx_` and the next eight characters, then its
hash is compared with `secrets.compare_digest`. The prefix is unique and not
secret; it is what lists, logs and the admin API show instead of the key.

Every reason a key is refused (unknown, revoked, owner disabled) gets the same
answer, so the response says nothing about which keys exist. The reason is
logged, without any of the key.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.models.identity import ApiKey, Plan, User

logger = get_logger(__name__)

KEY_SCHEME = "mtx_"
KEY_RANDOM_BYTES = 32
# The scheme plus 8 random characters: 48 bits, so prefixes collide about
# once in sixteen million keys, and `issue_key` simply draws again when they do.
PREFIX_RANDOM_CHARS = 8
PREFIX_LENGTH = len(KEY_SCHEME) + PREFIX_RANDOM_CHARS
ISSUE_ATTEMPTS = 5

INVALID_KEY = "Invalid API key."
MISSING_KEY = "Missing API key. Send it as `Authorization: Bearer mtx_...`."


@dataclass(frozen=True, slots=True)
class Principal:
    """Who a request is from: a user's key, or an anonymous caller by IP."""

    plan: Plan
    client_ip: str
    user_id: int | None = None
    key_id: int | None = None

    @property
    def anonymous(self) -> bool:
        return self.user_id is None

    @property
    def key(self) -> str:
        """A stable name for this caller, for ownership and per-caller limits."""
        return f"anon:{self.client_ip}" if self.anonymous else f"user:{self.user_id}"


def anonymous(client_ip: str) -> Principal:
    return Principal(plan=Plan.ANONYMOUS, client_ip=client_ip)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return KEY_SCHEME + secrets.token_urlsafe(KEY_RANDOM_BYTES)


async def issue_key(
    session: AsyncSession, user: User, *, label: str | None = None
) -> tuple[ApiKey, str]:
    """Create a key for `user`. Returns the row and the key itself, which is
    never stored and cannot be recovered later. Flushes; the caller commits."""
    for _ in range(ISSUE_ATTEMPTS):
        key = new_key()
        row = ApiKey(
            user_id=user.id, prefix=key[:PREFIX_LENGTH], key_hash=hash_key(key), label=label
        )
        try:
            async with session.begin_nested():
                session.add(row)
        except IntegrityError:  # the prefix is taken; draw another key
            continue
        logger.info("api_key_issued", user_id=user.id, key_id=row.id, prefix=row.prefix)
        return row, key
    raise RuntimeError("could not draw an unused API key prefix")  # pragma: no cover


async def authenticate(session: AsyncSession, key: str, client_ip: str) -> Principal:
    """The principal a key belongs to, or `AuthenticationError`."""
    if not key.startswith(KEY_SCHEME) or len(key) <= PREFIX_LENGTH:
        _refuse("malformed", client_ip)
    row = (
        await session.execute(
            sa.select(ApiKey, User)
            .join(User, User.id == ApiKey.user_id)
            .where(ApiKey.prefix == key[:PREFIX_LENGTH])
        )
    ).one_or_none()
    if row is None:
        _refuse("unknown_key", client_ip)
    api_key, user = row
    if not secrets.compare_digest(api_key.key_hash, hash_key(key)):
        _refuse("unknown_key", client_ip, key_id=api_key.id)
    if api_key.revoked_at is not None:
        _refuse("revoked", client_ip, key_id=api_key.id)
    if user.disabled:
        _refuse("user_disabled", client_ip, key_id=api_key.id)

    await _touch(session, api_key)
    return Principal(plan=user.plan, client_ip=client_ip, user_id=user.id, key_id=api_key.id)


def refuse_missing(client_ip: str) -> None:
    logger.info("auth_failed", reason="missing", client_ip=client_ip)
    raise AuthenticationError(MISSING_KEY)


def _refuse(reason: str, client_ip: str, *, key_id: int | None = None) -> None:
    logger.info("auth_failed", reason=reason, client_ip=client_ip, key_id=key_id)
    raise AuthenticationError(INVALID_KEY)


async def _touch(session: AsyncSession, api_key: ApiKey) -> None:
    """Record that the key was used, if it has not been recently. Commits."""
    now = datetime.now(timezone.utc)
    interval = timedelta(minutes=settings.api_key_touch_interval_minutes)
    last = api_key.last_used_at
    if last is not None and now - _as_utc(last) < interval:
        return
    # Conditional, so concurrent requests on one key write it once.
    await session.execute(
        sa.update(ApiKey)
        .where(
            ApiKey.id == api_key.id,
            sa.or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at < now - interval),
        )
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


def client_ip(peer: str | None, forwarded_for: str | None) -> str:
    """The caller's address: the socket peer, unless proxies are trusted.

    X-Forwarded-For is a list each proxy appends the address it received from
    to. Behind N trusted proxies, the Nth entry from the right was written by
    the outermost trusted proxy and is the real client; anything left of it
    the client could have written itself. With no trusted proxies the header
    is ignored entirely. If it has fewer entries than there are proxies, the
    request did not come through all of them, and the peer is used.
    """
    trusted = settings.trusted_proxy_count
    if trusted and forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if len(hops) >= trusted:
            return hops[-trusted]
    return peer or "unknown"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
