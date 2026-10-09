#!/usr/bin/env python3
"""Wait until the database schema is at least as new as this code's migrations.

The worker's pre-deploy command on Railway. The API and the worker deploy
separately from the same commit, and only the API's pre-deploy runs
`alembic upgrade head`: two services upgrading at once would race. Without
this, a new worker could start against the old schema while the API is still
migrating. With it, the worker's deploy waits for the migration, and if the
migration never lands (it failed, so the API's deploy stopped), the worker's
deploy fails too and the old worker keeps running.

    python scripts/wait_for_migrations.py --timeout 900

A database *ahead* of this code (a revision it does not know) counts as
ready: that is a rollback to the previous release, which every migration must
support (docs/DEPLOYMENT.md). Exits 0 when ready, 1 on timeout.

Uses DATABASE_URL like the app does.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from enum import Enum
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.session import dispose_engine, engine  # noqa: E402


class Schema(Enum):
    READY = "ready"  # at this code's head
    AHEAD = "ahead"  # newer than this code: a rollback, also fine
    BEHIND = "behind"  # older, or not migrated at all yet


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))


def compare(current: set[str], script: ScriptDirectory) -> Schema:
    """Where the database's revisions stand against this code's migrations."""
    if not current:
        return Schema.BEHIND
    known = {rev.revision for rev in script.walk_revisions()}
    if current - known:
        return Schema.AHEAD
    return Schema.READY if current == set(script.get_heads()) else Schema.BEHIND


async def current_revisions() -> set[str]:
    """The database's alembic revisions; empty before the first migration."""
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            return {row[0] for row in rows}
    except sa.exc.ProgrammingError:  # no alembic_version table yet
        return set()


async def wait(timeout: float, interval: float) -> bool:
    script = script_directory()
    heads = ", ".join(script.get_heads())
    deadline = time.monotonic() + timeout
    while True:
        try:
            current = await current_revisions()
            state = compare(current, script)
        except Exception as exc:  # database not reachable yet
            current, state = set(), Schema.BEHIND
            print(f"database not reachable: {type(exc).__name__}: {exc}", flush=True)
        if state is not Schema.BEHIND:
            print(f"schema {state.value}: database at {sorted(current)}, code at {heads}", flush=True)
            return True
        if time.monotonic() >= deadline:
            print(f"timed out: database at {sorted(current) or 'none'}, code needs {heads}", flush=True)
            return False
        print(f"waiting for migrations: database at {sorted(current) or 'none'}, code needs {heads}", flush=True)
        await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds to wait (default 900).")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between checks (default 5).")
    args = parser.parse_args(argv)

    async def run() -> bool:
        try:
            return await wait(args.timeout, args.interval)
        finally:
            await dispose_engine()

    return 0 if asyncio.run(run()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
