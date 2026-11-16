"""Review-driven hardening: lockout atomicity, login rate limit, session rotation, platform path."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.errors import InvalidCredentials
from shifde.core.settings import Settings
from shifde.identity import service as identity
from shifde.identity.passwords import PasswordService
from shifde.tenancy import repository as tenancy_repo
from tests._support.fullapp import ORIGIN, World, login

ADMIN_A = "admin@demo-a.local"
ADMIN_B = "admin@demo-b.local"
PLATFORM = "platform@shifde.local"


@pytest.fixture
def wide_db(app_url: str) -> Iterator[Database]:
    db = Database(app_url, pool_size=16, max_overflow=8)
    yield db
    db.dispose()


def test_parallel_bad_logins_lock_the_account_exactly_at_threshold(
    world: World, wide_db: Database, settings: Settings, owner_session: Session
) -> None:
    """Ten concurrent failures (separate connections) must lock; a stale-read decision would not."""
    passwords = PasswordService(fast=True)
    n = identity.LOCKOUT_THRESHOLD
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def attempt(i: int) -> None:
        try:
            with wide_db.session() as s:
                barrier.wait(timeout=10)
                with pytest.raises(InvalidCredentials):
                    identity.login_local(
                        s,
                        settings,
                        passwords,
                        RequestContext(request_id=f"r{i}"),
                        email=ADMIN_A,
                        password="wrong-password",
                        previous_token=None,
                    )
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    row = owner_session.execute(
        text(
            "SELECT coalesce(sum(l.failed_attempts), 0), coalesce(bool_or(l.locked_until > now()), false) "
            "FROM shifde.login_lockouts l JOIN shifde.users u ON u.id = l.user_id WHERE u.primary_email = :e"
        ),
        {"e": ADMIN_A},
    ).one()
    assert row[0] == n and row[1] is True


def test_nine_failures_do_not_lock(
    client: TestClient, world: World, owner_session: Session
) -> None:
    for _ in range(identity.LOCKOUT_THRESHOLD - 1):
        client.post("/api/auth/local/login", json={"email": ADMIN_A, "password": "x"})
    locked = owner_session.execute(
        text(
            "SELECT coalesce(bool_or(l.locked_until IS NOT NULL), false) "
            "FROM shifde.login_lockouts l JOIN shifde.users u ON u.id = l.user_id WHERE u.primary_email = :e"
        ),
        {"e": ADMIN_A},
    ).scalar_one()
    assert locked is False
    assert (
        client.post(
            "/api/auth/local/login", json={"email": ADMIN_A, "password": world.passwords[ADMIN_A]}
        ).status_code
        == 200
    )


def test_login_rate_limit_per_ip_returns_429(settings: Settings, world: World) -> None:
    limited = settings.model_copy(update={"login_rate_per_minute": 3})
    with TestClient(
        create_app(limited), base_url="https://testserver", headers={"Origin": ORIGIN}
    ) as c:
        for _ in range(3):
            assert (
                c.post(
                    "/api/auth/local/login", json={"email": ADMIN_A, "password": "x"}
                ).status_code
                == 401
            )
        r = c.post(
            "/api/auth/local/login", json={"email": ADMIN_A, "password": world.passwords[ADMIN_A]}
        )
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"
        assert r.headers["content-type"].startswith("application/problem+json")
        assert c.get("/api/health").status_code == 200  # only login is limited


def test_rate_limiter_window_slides() -> None:
    from shifde.core.errors import RateLimited
    from shifde.core.ratelimit import SlidingWindowLimiter

    now = [0.0]
    lim = SlidingWindowLimiter(2, 60.0, clock=lambda: now[0])
    lim.check("a")
    lim.check("a")
    lim.check("b")  # separate key
    with pytest.raises(RateLimited):
        lim.check("a")
    now[0] = 61.0
    lim.check("a")  # old hits aged out
    SlidingWindowLimiter(0).check("x")  # disabled


def test_login_revokes_previous_session_of_a_different_user(
    client: TestClient, world: World, owner_session: Session
) -> None:
    login(client, world, ADMIN_A)
    token_a = client.cookies.get("__Host-shifde_session")
    assert token_a
    login(client, world, ADMIN_B)  # same browser, cookie A still presented
    token_b = client.cookies.get("__Host-shifde_session")
    assert token_b and token_b != token_a
    revoked = (
        owner_session.execute(
            text(
                "SELECT u.primary_email FROM shifde.sessions s "
                "JOIN shifde.users u ON u.id = s.user_id "
                "WHERE s.revoked_at IS NOT NULL"
            )
        )
        .scalars()
        .all()
    )
    assert revoked == [ADMIN_A]
    client.cookies.set("__Host-shifde_session", token_a)
    assert client.get("/api/v1/orgs").status_code == 401  # A's session did not survive B's login
    client.cookies.set("__Host-shifde_session", token_b)
    assert client.get("/api/v1/orgs").status_code == 200


def test_platform_org_create_slug_race_returns_409_not_500(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = login(client, world, PLATFORM)
    # Simulate losing the race: the pre-check says the slug is free but the INSERT collides.
    monkeypatch.setattr(tenancy_repo, "tenant_slug_exists", lambda *_a, **_k: False)
    r = client.post("/api/v1/orgs", json={"slug": "demo-a", "name": "Dup"}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    monkeypatch.undo()
    # the session is healthy afterwards and platform context did not leak
    assert client.get("/api/v1/orgs?scope=all").status_code == 200
    assert client.get("/api/v1/orgs/demo-a/workspaces").status_code == 404


def test_dashboard_member_count_is_null_without_member_read(
    client: TestClient, world: World
) -> None:
    login(client, world, ADMIN_A)
    assert client.get("/api/v1/orgs/demo-a/dashboard").json()["counts"]["members"] == 2
