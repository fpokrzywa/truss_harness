"""Re-review fixes: trusted proxies, account throttle, lockout serialization, un-revoke."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core import db
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.errors import InvalidCredentials
from shifde.core.ratelimit import ip_limit_applies
from shifde.core.settings import Settings
from shifde.identity import service as identity
from shifde.identity.passwords import PasswordService
from tests._support.fullapp import ORIGIN, World, login

ADMIN_A = "admin@demo-a.local"
VIEWER_A = "viewer@demo-a.local"
EVIL_XFF = {"X-Forwarded-For": "6.6.6.6"}


def _client(settings: Settings, **kw: object) -> TestClient:
    return TestClient(
        create_app(settings), base_url="https://testserver", headers={"Origin": ORIGIN}, **kw
    )  # type: ignore[arg-type]  # SETH test helper: TestClient options as object


def _ips(owner: Session) -> tuple[set[str | None], set[str | None]]:
    audit = {
        r[0]
        for r in owner.execute(
            text("SELECT actor_ip FROM shifde.audit_events WHERE action LIKE 'identity.%'")
        )
    }
    sess = {r[0] for r in owner.execute(text("SELECT ip FROM shifde.sessions"))}
    return audit, sess


# ------------------------------------------------------------------ proxy headers / IP keys
def test_spoofed_xff_from_untrusted_peer_changes_neither_key_nor_recorded_ip(
    settings: Settings, world: World, owner_session: Session
) -> None:
    limited = settings.model_copy(update={"login_rate_per_minute": 3})
    with _client(limited) as c:
        creds = {"email": ADMIN_A, "password": world.passwords[ADMIN_A]}
        ok = c.post("/api/auth/local/login", json=creds, headers=EVIL_XFF)
        assert ok.status_code == 200
        audit_ips, session_ips = _ips(owner_session)
        assert audit_ips == {"testclient"} and session_ips == {"testclient"}
        # a fresh spoofed address per request does not buy a fresh limiter key
        for i in range(2):
            c.post("/api/auth/local/login", json=creds, headers={"X-Forwarded-For": f"7.7.7.{i}"})
        r = c.post("/api/auth/local/login", json=creds, headers={"X-Forwarded-For": "8.8.8.8"})
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"


def test_trusted_proxy_xff_is_honoured_for_ip_and_limiter(
    settings: Settings, world: World, owner_session: Session
) -> None:
    cfg = settings.model_copy(update={"trusted_proxies": ["127.0.0.1"], "login_rate_per_minute": 2})
    with _client(cfg, client=("127.0.0.1", 4242)) as c:
        creds = {"email": ADMIN_A, "password": world.passwords[ADMIN_A]}
        ok = c.post("/api/auth/local/login", json=creds, headers={"X-Forwarded-For": "9.9.9.9"})
        assert ok.status_code == 200
        audit_ips, session_ips = _ips(owner_session)
        assert session_ips == {"9.9.9.9"} and audit_ips == {"9.9.9.9"}
        c.post("/api/auth/local/login", json=creds, headers={"X-Forwarded-For": "9.9.9.9"})
        third = c.post("/api/auth/local/login", json=creds, headers={"X-Forwarded-For": "9.9.9.9"})
        assert third.status_code == 429  # limiter keys on the forwarded client address
        other = c.post("/api/auth/local/login", json=creds, headers={"X-Forwarded-For": "9.9.9.8"})
        assert other.status_code == 200  # a different real client is unaffected


def test_the_same_host_proxy_is_believed_only_when_loopback_trust_is_on(
    settings: Settings, world: World, owner_session: Session
) -> None:
    """Core review 1 item 4 (B-201, B-2250): the frontend proxy on this host (a loopback peer)
    sets the client address only when `trust_loopback_proxy` is on (B-2280: default off); off, it is
    ignored, so a client that
    reaches the app over loopback by another route cannot pick its own address."""
    creds = {"email": ADMIN_A, "password": world.passwords[ADMIN_A]}
    default = settings.model_copy(update={"login_rate_per_minute": 2})
    assert default.trust_loopback_proxy is False  # B-2280: not believed until the operator opts in
    on = default.model_copy(update={"trust_loopback_proxy": True})
    with _client(on, client=("127.0.0.1", 4242)) as c:
        assert c.post("/api/auth/local/login", json=creds, headers=EVIL_XFF).status_code == 200
    audit_ips, session_ips = _ips(owner_session)
    assert audit_ips == {"6.6.6.6"} and session_ips == {"6.6.6.6"}
    owner_session.execute(text("DELETE FROM shifde.sessions"))
    owner_session.commit()
    off = default
    with _client(off, client=("127.0.0.1", 4242)) as c:
        assert c.post("/api/auth/local/login", json=creds, headers=EVIL_XFF).status_code == 200
    _, session_ips = _ips(owner_session)
    assert session_ips == {"127.0.0.1"}
    with _client(
        off.model_copy(update={"trusted_proxies": ["127.0.0.1"]}), client=("127.0.0.1", 4242)
    ) as c:  # a listed proxy is trusted whether or not loopback is
        assert c.post("/api/auth/local/login", json=creds, headers=EVIL_XFF).status_code == 200
    assert "6.6.6.6" in _ips(owner_session)[1]


def test_a_forwarded_loopback_address_is_still_limited_through_a_loopback_peer(
    settings: Settings, world: World
) -> None:
    """App isolation review 2 item 1: `X-Forwarded-For: 127.0.0.1` must not switch the per-IP
    limit off, and rotating values must not buy a fresh key, from a loopback peer in either trust
    mode."""
    for trust in (False, True):
        cfg = settings.model_copy(
            update={"login_rate_per_minute": 3, "trust_loopback_proxy": trust}
        )
        body = {"email": ADMIN_A, "password": "wrong"}
        with _client(cfg, client=("127.0.0.1", 1)) as c:
            codes = [
                c.post(
                    "/api/auth/local/login", json=body, headers={"X-Forwarded-For": "127.0.0.1"}
                ).status_code
                for _ in range(5)
            ]
        assert codes[:3] == [401] * 3 and codes[3:] == [429] * 2, (trust, codes)
    rotating = settings.model_copy(update={"login_rate_per_minute": 3})
    with _client(rotating, client=("127.0.0.1", 1)) as c:
        codes = [
            c.post(
                "/api/auth/local/login", json=body, headers={"X-Forwarded-For": f"7.7.{i}.9"}
            ).status_code
            for i in range(6)
        ]
    assert codes == [401, 401, 401, 429, 429, 429]
    assert ip_limit_applies("127.0.0.1", [], forwarded=True) is True
    assert ip_limit_applies("10.0.0.5", ["10.0.0.0/24"], forwarded=True) is True
    assert ip_limit_applies("::1", [], forwarded=False) is False


def test_loopback_and_trusted_proxy_peers_skip_the_per_ip_limit(
    settings: Settings, world: World
) -> None:
    cfg = settings.model_copy(update={"login_rate_per_minute": 2})
    with _client(cfg, client=("127.0.0.1", 1)) as c:  # no XFF: peer is the loopback proxy
        codes = [
            c.post("/api/auth/local/login", json={"email": ADMIN_A, "password": "x"}).status_code
            for _ in range(6)
        ]
        assert codes == [401] * 6  # shared loopback key can no longer lock everyone out
    assert ip_limit_applies("127.0.0.1", []) is False
    assert ip_limit_applies("::1", []) is False
    assert ip_limit_applies("10.0.0.5", ["10.0.0.0/24"]) is False
    assert ip_limit_applies("10.0.1.5", ["10.0.0.0/24"]) is True
    assert ip_limit_applies("203.0.113.9", []) is True


def test_trusted_proxies_setting_parsing_and_validation() -> None:
    base = {"_env_file": None, "database_url": "postgresql+psycopg://x", "env": "test"}
    s = Settings(**base, trusted_proxies="127.0.0.1, 10.0.0.0/8")
    assert s.trusted_proxies == ["127.0.0.1", "10.0.0.0/8"]
    assert Settings(**base).trusted_proxies == []
    with pytest.raises(ValidationError):
        Settings(**base, trusted_proxies="not-an-ip")


# ------------------------------------------------------------------ per-account throttle
def test_per_account_limit_trips_for_one_email_without_affecting_another(
    settings: Settings, world: World
) -> None:
    cfg = settings.model_copy(update={"login_rate_per_account_per_minute": 3})
    with _client(cfg, client=("127.0.0.1", 1)) as c:
        for _ in range(3):
            r = c.post("/api/auth/local/login", json={"email": ADMIN_A, "password": "x"})
            assert r.status_code == 401
        r = c.post("/api/auth/local/login", json={"email": ADMIN_A.upper(), "password": "x"})
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"  # normalized key
        ok = c.post(
            "/api/auth/local/login",
            json={"email": VIEWER_A, "password": world.passwords[VIEWER_A]},
        )
        assert ok.status_code == 200


# ------------------------------------------------------------------ lockout serialization
@pytest.fixture
def wide_db(app_url: str) -> Iterator[Database]:
    d = Database(app_url, pool_size=16, max_overflow=8)
    yield d
    d.dispose()


def _run_parallel(
    wide_db: Database, settings: Settings, password_for: list[str], correct: str
) -> list[str]:
    passwords = PasswordService(fast=True)
    barrier = threading.Barrier(len(password_for))
    outcomes: list[str] = [""] * len(password_for)

    def attempt(i: int) -> None:
        with wide_db.session() as s:
            barrier.wait(timeout=10)
            try:
                identity.login_local(
                    s,
                    settings,
                    passwords,
                    RequestContext(request_id=f"r{i}"),
                    email=ADMIN_A,
                    password=password_for[i],
                    previous_token=None,
                )
                outcomes[i] = "ok" if password_for[i] == correct else "ok-wrong?!"
            except InvalidCredentials:
                outcomes[i] = "rejected"
            except BaseException as exc:  # noqa: BLE001
                outcomes[i] = f"error:{exc!r}"

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(len(password_for))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return outcomes


def test_correct_password_is_rejected_while_locked_even_when_racing(
    world: World, wide_db: Database, settings: Settings, owner_session: Session
) -> None:
    n = identity.LOCKOUT_THRESHOLD
    first = _run_parallel(wide_db, settings, ["bad"] * n, world.passwords[ADMIN_A])
    assert first == ["rejected"] * n
    good = world.passwords[ADMIN_A]
    second = _run_parallel(wide_db, settings, ["bad", "bad", good, "bad", good, "bad"], good)
    assert second == ["rejected"] * 6  # nothing is checked once locked, not even the right one
    row = owner_session.execute(
        text(
            "SELECT coalesce(sum(l.failed_attempts), 0), coalesce(bool_or(l.locked_until > now()), false) "
            "FROM shifde.login_lockouts l JOIN shifde.users u ON u.id = l.user_id WHERE u.primary_email = :e"
        ),
        {"e": ADMIN_A},
    ).one()
    assert row[0] == n + 6 and row[1] is True
    assert owner_session.execute(text("SELECT count(*) FROM shifde.sessions")).scalar_one() == 0


def test_race_around_threshold_never_admits_after_lock(
    world: World, wide_db: Database, settings: Settings, owner_session: Session
) -> None:
    """9 failures first, then a 1-wrong + 1-right race: the wrong one may lock the account before
    the right one is checked, but a lock must then hold; either order leaves a consistent state."""
    for _ in range(identity.LOCKOUT_THRESHOLD - 1):
        with wide_db.session() as s, pytest.raises(InvalidCredentials):
            identity.login_local(
                s,
                settings,
                PasswordService(fast=True),
                RequestContext(request_id="r"),
                email=ADMIN_A,
                password="bad",
                previous_token=None,
            )
    good = world.passwords[ADMIN_A]
    out = _run_parallel(wide_db, settings, ["bad", good], good)
    assert sorted(out) in (["ok", "rejected"], ["rejected", "rejected"])
    locked = owner_session.execute(
        text(
            "SELECT coalesce(bool_or(l.locked_until IS NOT NULL), false) "
            "FROM shifde.login_lockouts l JOIN shifde.users u ON u.id = l.user_id WHERE u.primary_email = :e"
        ),
        {"e": ADMIN_A},
    ).scalar_one()
    # right-then-wrong resets the counter (no lock); wrong-then-right locks and rejects both
    assert locked == (out == ["rejected", "rejected"])


# ------------------------------------------------------------------ sessions cannot be un-revoked
def test_revoked_session_cannot_be_unrevoked(
    client: TestClient, world: World, owner_session: Session, app_db: Database
) -> None:
    headers = login(client, world, ADMIN_A)
    client.post("/api/auth/logout", headers=headers)
    sid, uid = owner_session.execute(text("SELECT id, user_id FROM shifde.sessions")).one()
    with pytest.raises(DBAPIError, match="cannot be reinstated"):
        owner_session.execute(
            text("UPDATE shifde.sessions SET revoked_at = NULL WHERE id = :i"), {"i": sid}
        )
    owner_session.rollback()
    with app_db.session() as s:
        db.set_principal(s, uid)
        with pytest.raises(DBAPIError, match="cannot be reinstated"):
            s.execute(
                text("UPDATE shifde.sessions SET revoked_at = NULL WHERE id = :i"), {"i": sid}
            )
        s.rollback()
    still = owner_session.execute(
        text("SELECT revoked_at IS NOT NULL FROM shifde.sessions WHERE id = :i"), {"i": sid}
    ).scalar_one()
    assert still is True
    # an active session can still be revoked, and revoked_at can be re-stamped
    headers = login(client, world, ADMIN_A)
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
