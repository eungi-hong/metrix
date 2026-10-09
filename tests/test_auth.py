"""Tests for identity: API keys, the 401s, anonymous callers, client IPs, who
may see which job and conversation, the admin user and key endpoints, and the
bootstrap script."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.core.context import CallClass
from app.models.chat import ChatMessage, Conversation
from app.models.enums import MessageRole
from app.models.identity import ApiKey, Plan
from app.models.jobs import JobKind, JobSource
from app.models.usage import UsageEvent
from app.services import auth, queue, spend
from app.worker import Worker
from tests.conftest import StubLLM, api_app, api_client, new_api_key

ADMIN = {"X-Admin-Token": "s3cret"}


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")


@pytest.fixture
def app(session_factory, stub_llm):
    return api_app(session_factory, stub_llm)


@pytest.fixture
def anonymous_allowed(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)


async def as_new_user(session_factory, app, plan: Plan = Plan.PRO):
    _, key = await new_api_key(session_factory, plan=plan, name=f"user-{plan}")
    return api_client(app, key)


# --------------------------------------------------------------- the keys


async def test_a_key_is_stored_only_as_its_hash(session_factory):
    user, key = await new_api_key(session_factory)

    async with session_factory() as session:
        (row,) = (await session.scalars(sa.select(ApiKey))).all()
    assert key.startswith("mtx_") and len(key) > 40
    assert row.key_hash == hashlib.sha256(key.encode()).hexdigest()
    assert key not in (row.key_hash, row.prefix, row.label or "")
    assert row.prefix == key[:12] and row.prefix.startswith("mtx_")


def test_keys_are_random_and_their_prefixes_distinct():
    keys = {auth.new_key() for _ in range(500)}
    assert len(keys) == 500
    assert len({key[: auth.PREFIX_LENGTH] for key in keys}) == 500


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (None, "no key"),
        ("Bearer mtx_" + "A" * 43, "a well-formed key nobody issued"),
        ("Bearer not-a-key", "not our format"),
        ("Basic dXNlcjpwYXNz", "another scheme"),
    ],
)
async def test_requests_without_a_valid_key_are_401(app, header, why):
    headers = {"Authorization": header} if header else {}
    async with api_client(app) as client:
        response = await client.get("/tickers/AAPL", headers=headers)

    assert response.status_code == 401, why
    assert response.json()["error"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_the_right_prefix_with_the_wrong_secret_is_refused(app, session_factory):
    _, key = await new_api_key(session_factory)
    forged = key[: auth.PREFIX_LENGTH] + "x" * (len(key) - auth.PREFIX_LENGTH)
    async with api_client(app, forged) as client:
        assert (await client.get("/tickers/AAPL")).status_code == 401


async def test_every_protected_route_needs_a_key_and_health_does_not(app):
    async with api_client(app) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/tickers/AAPL")).status_code == 401
        assert (await client.post("/chat", json={"question": "hi"})).status_code == 401
        assert (await client.get("/jobs/1")).status_code == 401
        assert (await client.get("/conversations")).status_code == 401
        assert (await client.get("/conversations/x")).status_code == 401


async def test_a_revoked_key_stops_working_and_others_do_not(app, session_factory):
    user, key = await new_api_key(session_factory)
    async with api_client(app) as admin:
        spare = (await admin.post(f"/admin/users/{user.id}/keys", json={"label": "spare"}, headers=ADMIN)).json()
        key_id = await _key_id(session_factory, key)
        revoked = await admin.delete(f"/admin/keys/{key_id}", headers=ADMIN)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]

    async with api_client(app, key) as client:
        assert (await client.get("/tickers/AAPL")).status_code == 401
    async with api_client(app, spare["key"]) as client:
        assert (await client.get("/tickers/AAPL")).status_code != 401


async def test_a_disabled_user_is_refused_until_re_enabled(app, session_factory):
    user, key = await new_api_key(session_factory)
    async with api_client(app) as admin:
        assert (await admin.patch(f"/admin/users/{user.id}", json={"disabled": True}, headers=ADMIN)).json()["disabled_at"]
        async with api_client(app, key) as client:
            assert (await client.get("/tickers/AAPL")).status_code == 401
        await admin.patch(f"/admin/users/{user.id}", json={"disabled": False}, headers=ADMIN)
        async with api_client(app, key) as client:
            assert (await client.get("/tickers/AAPL")).status_code != 401


async def test_auth_failures_are_logged_without_the_key(app, session_factory, monkeypatch):
    events = []
    monkeypatch.setattr(auth.logger, "info", lambda event, **fields: events.append((event, fields)))
    _, key = await new_api_key(session_factory)
    async with api_client(app, key[:-1] + ("A" if key[-1] != "A" else "B")) as client:
        await client.get("/tickers/AAPL")

    (event, fields) = next(e for e in events if e[0] == "auth_failed")
    assert fields["reason"] == "unknown_key"
    assert not any(key[4:] in str(value) or key in str(value) for value in fields.values())


async def test_last_used_is_written_at_most_once_per_interval(app, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_key_touch_interval_minutes", 5)
    _, key = await new_api_key(session_factory)

    async with api_client(app, key) as client:
        await client.get("/conversations")
        first = await _last_used(session_factory, key)
        await client.get("/conversations")
        assert await _last_used(session_factory, key) == first

        async with session_factory() as session:  # pretend the last use was long ago
            await session.execute(
                sa.update(ApiKey).values(last_used_at=datetime.now(timezone.utc) - timedelta(minutes=6))
            )
            await session.commit()
        await client.get("/conversations")
        assert await _last_used(session_factory, key) > first
    assert first is not None


# ------------------------------------------------------------ anonymous


async def test_without_auth_required_an_unkeyed_caller_is_anonymous(app, anonymous_allowed):
    async with api_client(app) as client:
        response = await client.post("/chat", json={"question": "What moved?"})
        assert response.status_code == 200
        conversation = response.json()["conversation_id"]
        assert (await client.get(f"/conversations/{conversation}")).status_code == 200


async def test_without_auth_required_a_bad_key_is_still_refused(app, anonymous_allowed):
    async with api_client(app, "mtx_" + "Z" * 43) as client:
        assert (await client.get("/tickers/AAPL")).status_code == 401


async def test_anonymous_callers_are_told_apart_by_ip_and_xff_is_not_trusted(
    app, session_factory, anonymous_allowed
):
    async with api_client(app) as client:
        conversation = (await client.post("/chat", json={"question": "What moved?"})).json()["conversation_id"]
        spoofed = await client.get(
            f"/conversations/{conversation}", headers={"X-Forwarded-For": "203.0.113.9"}
        )
    assert spoofed.status_code == 200, "an untrusted header does not change who you are"

    async with session_factory() as session:
        stored = await session.get(Conversation, conversation)
    assert stored.user_id is None and stored.anonymous_key == "anon:127.0.0.1"


@pytest.mark.parametrize(
    ("trusted", "header", "peer", "expected"),
    [
        (0, "1.1.1.1, 2.2.2.2", "10.0.0.1", "10.0.0.1"),  # ignored by default
        (1, "1.1.1.1, 2.2.2.2", "10.0.0.1", "2.2.2.2"),  # the one our proxy saw
        (2, "6.6.6.6, 1.1.1.1, 2.2.2.2", "10.0.0.1", "1.1.1.1"),  # 6.6.6.6 is client-written
        (2, "2.2.2.2", "10.0.0.1", "10.0.0.1"),  # fewer hops than proxies: the peer
        (1, None, "10.0.0.1", "10.0.0.1"),
        (1, " , 2.2.2.2 ", "10.0.0.1", "2.2.2.2"),
    ],
)
def test_client_ip(monkeypatch, trusted, header, peer, expected):
    monkeypatch.setattr(settings, "trusted_proxy_count", trusted)
    assert auth.client_ip(peer, header) == expected


# ------------------------------------------------------------------ jobs


async def test_a_job_is_visible_only_to_the_callers_it_serves(app, session_factory):
    async with await as_new_user(session_factory, app) as alice, await as_new_user(
        session_factory, app
    ) as bob:
        job_id = (await alice.get("/tickers/COLD")).json()["job_id"]
        assert (await alice.get(f"/jobs/{job_id}")).status_code == 200
        assert (await bob.get(f"/jobs/{job_id}")).status_code == 404

        # Bob asks for the same ticker: his request joins Alice's job.
        assert (await bob.get("/tickers/COLD")).json()["job_id"] == job_id
        assert (await bob.get(f"/jobs/{job_id}")).status_code == 200


async def test_nightly_jobs_are_admin_only(app, session_factory, client):
    async with session_factory() as session:
        job = await queue.enqueue(session, JobKind.SCHEDULE_NIGHTLY, {}, priority=5,
                                  dedupe_key="nightly:x", source=JobSource.SCHEDULED)
        await session.commit()

    assert (await client.get(f"/jobs/{job.id}")).status_code == 404
    assert (await client.get(f"/jobs/{job.id}", headers=ADMIN)).status_code == 200
    assert (await client.get(f"/jobs/{job.id}", headers={"X-Admin-Token": "wrong"})).status_code == 401


async def test_the_job_remembers_who_created_it(app, session_factory, user_key, client):
    job_id = (await client.get("/tickers/COLD")).json()["job_id"]
    async with session_factory() as session:
        job = await queue.active_job(session, queue.ingest_key("COLD"))
    assert job.id == job_id and job.user_id == user_key[0].id


async def test_a_users_job_spends_as_that_user(db, ledger):
    user, _ = await new_api_key(db)

    async def handler(session, job, ctx) -> None:
        async with spend.metered("exa", "search", Decimal("0.01")) as meter:
            await meter.settle(cost_usd=Decimal("0.01"), estimated=False)

    async with db() as session:
        job = await queue.enqueue(session, JobKind.INGEST_TICKER, {}, priority=0,
                                  dedupe_key="ingest:X", source=JobSource.INTERACTIVE, user_id=user.id)
        await session.commit()
    await Worker(session_factory=db, llm=StubLLM(), handlers={JobKind.INGEST_TICKER: handler}).run_once()

    async with ledger() as session:
        (row,) = (await session.scalars(sa.select(UsageEvent))).all()
    assert (row.user_id, row.job_id, row.call_class) == (user.id, job.id, CallClass.INTERACTIVE)


# ---------------------------------------------------------- conversations


async def test_only_the_owner_can_continue_or_read_a_conversation(app, session_factory):
    async with await as_new_user(session_factory, app) as alice, await as_new_user(
        session_factory, app
    ) as bob:
        conversation = (await alice.post("/chat", json={"question": "Why did COLD drop?"})).json()["conversation_id"]

        follow_up = {"question": "And then?", "conversation_id": conversation}
        assert (await alice.post("/chat", json=follow_up)).status_code == 200
        assert (await bob.post("/chat", json=follow_up)).status_code == 404
        assert (await bob.get(f"/conversations/{conversation}")).status_code == 404

        unknown = {"question": "Hello?", "conversation_id": "no-such-conversation"}
        assert (await bob.post("/chat", json=unknown)).status_code == 404, "same answer as not yours"


async def test_a_conversation_from_before_ownership_is_admin_only(session_factory, client):
    async with session_factory() as session:
        legacy = Conversation()
        session.add(legacy)
        await session.flush()
        session.add(ChatMessage(conversation_id=legacy.id, role=MessageRole.USER, content="old"))
        await session.commit()

    assert (await client.get(f"/conversations/{legacy.id}")).status_code == 404
    assert (await client.post("/chat", json={"question": "q", "conversation_id": legacy.id})).status_code == 404
    body = (await client.get(f"/conversations/{legacy.id}", headers=ADMIN)).json()
    assert [m["content"] for m in body["messages"]] == ["old"]


async def test_listing_shows_your_own_newest_first_and_pages(app, session_factory):
    async with await as_new_user(session_factory, app) as alice, await as_new_user(
        session_factory, app
    ) as bob:
        ids = [
            (await alice.post("/chat", json={"question": f"Question {i}?"})).json()["conversation_id"]
            for i in range(3)
        ]
        await bob.post("/chat", json={"question": "Bob's question?"})
        await alice.post("/chat", json={"question": "More?", "conversation_id": ids[0]})

        page = (await alice.get("/conversations", params={"limit": 2})).json()
        rest = (await alice.get("/conversations", params={"limit": 2, "offset": 2})).json()

    assert page["total"] == 3
    listed = [c["id"] for c in page["conversations"] + rest["conversations"]]
    assert sorted(listed) == sorted(ids)
    assert listed[0] == ids[0], "the one continued last comes first"
    assert page["conversations"][0]["messages"] == 4


async def test_a_conversation_comes_back_with_its_messages_and_sources(client):
    first = (await client.post("/chat", json={"question": "Why did COLD move?"})).json()

    body = (await client.get(f"/conversations/{first['conversation_id']}")).json()

    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "Why did COLD move?"
    assert body["messages"][1]["content"] == first["answer"]
    assert body["messages"][1]["sources"] == first["sources"]


# ---------------------------------------------------------------- admin


async def test_admin_creates_users_and_issues_keys_shown_once(app, session_factory):
    async with api_client(app) as admin:
        created = await admin.post("/admin/users", json={"name": "Ada", "email": "ada@example.com", "plan": "pro"}, headers=ADMIN)
        assert created.status_code == 201
        user = created.json()
        assert user["plan"] == "pro" and user["disabled_at"] is None

        dup = await admin.post("/admin/users", json={"name": "Ada 2", "email": "ada@example.com"}, headers=ADMIN)
        assert dup.status_code == 409

        issued = (await admin.post(f"/admin/users/{user['id']}/keys", json={"label": "laptop"}, headers=ADMIN)).json()
        assert issued["key"].startswith(issued["prefix"]) and issued["label"] == "laptop"

        changed = await admin.patch(f"/admin/users/{user['id']}", json={"plan": "internal"}, headers=ADMIN)
        assert changed.json()["plan"] == "internal"

        assert (await admin.post("/admin/users", json={"name": "x", "plan": "anonymous"}, headers=ADMIN)).status_code == 422
        assert (await admin.post("/admin/users/999/keys", json={}, headers=ADMIN)).status_code == 404
        assert (await admin.delete("/admin/keys/999", headers=ADMIN)).status_code == 404
        assert (await admin.post("/admin/users", json={"name": "x"})).status_code == 401

    async with api_client(app, issued["key"]) as client:
        assert (await client.get("/conversations")).status_code == 200


async def test_the_bootstrap_script_creates_a_user_and_a_working_key(session_factory, monkeypatch, capsys):
    from scripts import create_api_key

    monkeypatch.setattr(create_api_key, "SessionLocal", session_factory)
    user, prefix, key, created = await create_api_key.create("Ada", "ada@example.com", "pro", "cli")
    again = await create_api_key.create("Ignored", "ada@example.com", "free", None)

    assert created and not again[3] and again[0].id == user.id
    async with session_factory() as session:
        principal = await auth.authenticate(session, key, "127.0.0.1")
        assert (principal.user_id, principal.plan) == (user.id, Plan.PRO)
        assert len((await session.scalars(sa.select(ApiKey))).all()) == 2


# --------------------------------------------------------------- helpers


async def _key_id(session_factory, key: str) -> int:
    async with session_factory() as session:
        return await session.scalar(sa.select(ApiKey.id).where(ApiKey.prefix == key[: auth.PREFIX_LENGTH]))


async def _last_used(session_factory, key: str) -> datetime | None:
    async with session_factory() as session:
        return await session.scalar(
            sa.select(ApiKey.last_used_at).where(ApiKey.prefix == key[: auth.PREFIX_LENGTH])
        )
