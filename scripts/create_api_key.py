#!/usr/bin/env python3
"""Create a user and an API key straight in the database, no API needed.

For the first key on a fresh deploy (the admin API needs ADMIN_TOKEN, and
everything else needs a key), and for scripts. Reuses the user if one with
the given email exists. The key is printed once and never stored.

    python scripts/create_api_key.py --name "Ada" --email ada@example.com --plan pro
    docker compose exec api python scripts/create_api_key.py --name "Ada"

Uses DATABASE_URL like the app does.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal, dispose_engine  # noqa: E402
from app.models.identity import Plan, User  # noqa: E402
from app.services import auth  # noqa: E402

STORED_PLANS = [plan.value for plan in Plan if plan != Plan.ANONYMOUS]


async def create(name: str, email: str | None, plan: str, label: str | None) -> tuple[User, str, str, bool]:
    async with SessionLocal() as session:
        user = None
        if email:
            user = await session.scalar(sa.select(User).where(User.email == email))
        created = user is None
        if user is None:
            user = User(name=name, email=email, plan=Plan(plan))
            session.add(user)
            await session.flush()
        row, key = await auth.issue_key(session, user, label=label)
        await session.commit()
        return user, row.prefix, key, created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", help="Optional. If a user has it already, they get the new key.")
    parser.add_argument("--plan", choices=STORED_PLANS, default=Plan.FREE.value)
    parser.add_argument("--label", help="What the key is for, e.g. 'laptop'.")
    args = parser.parse_args(argv)

    async def run() -> tuple[User, str, str, bool]:
        try:
            return await create(args.name, args.email, args.plan, args.label)
        finally:
            await dispose_engine()

    user, prefix, key, created = asyncio.run(run())
    print(f"{'Created' if created else 'Found'} user {user.id} ({user.name}, plan {user.plan.value}).")
    print(f"API key {prefix}... -- shown once, store it now:\n\n{key}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
