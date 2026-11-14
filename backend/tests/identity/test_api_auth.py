"""Authentication routes, sessions, CSRF and Origin checks (SETH 3ba0e06 `test_api_auth.py`).

Moved with identity part 2b over the minimal app of `tests/identity/harness.py` (B-920): the
assertions are SETH's; the SETH `World` admin (`admin@demo-a.local`, seeded through tenancy) is a
person with a membership in one client, and SETH's `/api/v1/orgs` and workspace routes are the
`require_auth` probe routes. Changed with a reason: the lockout counter is per (person, source)
since B-911 (`login_lockouts`, not `local_credentials`). SETH tests of other modules' routes
(health, readiness, request id, problem shape, logs, OpenAPI, every `/api/v1` route) stay with the
`app` port (carry_forward).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.tenants import add_tenant, bind_user
from tests.identity.conftest import owner_membership
from tests.identity.harness import COOKIE, ORIGIN, PASSWORD, client_for, login, person

ADMIN_A = "admin@demo-a.local"
WHOAMI = "/api/v1/probe/whoami"


@pytest.fixture
def admin_a(owner_session: Session) -> UUID:
    uid = person(owner_session, ADMIN_A)
    tenant = add_tenant(owner_session)
    owner_membership(owner_session, tenant, uid)
    bind_user(owner_session, tenant, uid)  # a client is listed only with a binding (B-1009)
    owner_session.commit()
    return uid


def _probe_post(org: UUID) -> str:
    return f"/api/v1/probe/orgs/{org}/api-keys/{uuid7()}"


def test_login_success_sets_hardened_cookie_and_returns_session(
    auth_client: TestClient, admin_a: UUID
) -> None:
    r = auth_client.post("/api/auth/local/login", json={"email": ADMIN_A, "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == ADMIN_A and body["user"]["is_platform_admin"] is False
    assert body["user"]["id"] == str(admin_a)
    assert body["csrf_token"] and body["expires_at"]
    assert len(body["memberships"]) == 1
    cookie = r.headers["set-cookie"].lower()
    assert "__host-shifde_session=" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "path=/" in cookie
    again = auth_client.get("/api/auth/session")
    assert again.status_code == 200
    assert again.json()["csrf_token"] == body["csrf_token"]


def test_login_failures_are_generic_and_do_not_reveal_account_existence(
    auth_client: TestClient, admin_a: UUID
) -> None:
    wrong_pw = auth_client.post("/api/auth/local/login", json={"email": ADMIN_A, "password": "no"})
    unknown = auth_client.post(
        "/api/auth/local/login", json={"email": "ghost@demo-a.local", "password": "nope"}
    )
    for r in (wrong_pw, unknown):
        assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"
    assert wrong_pw.json()["detail"] == unknown.json()["detail"]
    assert "set-cookie" not in wrong_pw.headers


def test_lockout_counter_and_lock_after_ten_failures(
    auth_client: TestClient, admin_a: UUID, owner_session: Session
) -> None:
    for _ in range(10):
        assert (
            auth_client.post(
                "/api/auth/local/login", json={"email": ADMIN_A, "password": "wrong"}
            ).status_code
            == 401
        )
    row = owner_session.execute(
        text("SELECT failed_attempts, locked_until FROM shifde.login_lockouts WHERE user_id = :u"),
        {"u": admin_a},
    ).one()
    assert row[0] == 10 and row[1] is not None
    # while locked, even the right password is refused (generic 401)
    r = auth_client.post("/api/auth/local/login", json={"email": ADMIN_A, "password": PASSWORD})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


def test_successful_login_resets_failure_counter(
    auth_client: TestClient, admin_a: UUID, owner_session: Session
) -> None:
    auth_client.post("/api/auth/local/login", json={"email": ADMIN_A, "password": "wrong"})
    login(auth_client, ADMIN_A)
    n = owner_session.execute(
        text("SELECT max(failed_attempts) FROM shifde.login_lockouts")
    ).scalar_one()
    assert n == 0


def test_login_and_logout_are_audited(
    auth_client: TestClient, admin_a: UUID, owner_session: Session
) -> None:
    auth_client.post("/api/auth/local/login", json={"email": ADMIN_A, "password": "wrong"})
    auth_client.post("/api/auth/local/login", json={"email": "ghost@x.local", "password": "wrong"})
    headers = login(auth_client, ADMIN_A)
    auth_client.post("/api/auth/logout", headers=headers)
    rows = owner_session.execute(
        text("SELECT action, outcome, tenant_id FROM shifde.audit_events ORDER BY occurred_at")
    ).all()
    actions = [(r[0], r[1]) for r in rows]
    assert ("identity.login.failed", "failure") in actions
    assert ("identity.login.succeeded", "success") in actions
    assert ("identity.logout", "success") in actions
    unknown = [r for r in rows if r[0] == "identity.login.failed" and r[2] is None]
    assert len(unknown) == 1  # unknown account -> installation-level event
    dumped = str(owner_session.execute(text("SELECT details FROM shifde.audit_events")).all())
    assert "wrong" not in dumped and PASSWORD not in dumped


def test_logout_revokes_the_server_side_session(auth_client: TestClient, admin_a: UUID) -> None:
    headers = login(auth_client, ADMIN_A)
    token = auth_client.cookies.get(COOKIE)
    assert token
    r = auth_client.post("/api/auth/logout", headers=headers)
    assert r.status_code == 204
    auth_client.cookies.set(COOKIE, token)  # replay the old cookie
    assert auth_client.get(WHOAMI).status_code == 401


def test_session_idle_and_absolute_expiry(
    auth_client: TestClient, admin_a: UUID, owner_session: Session
) -> None:
    login(auth_client, ADMIN_A)
    assert auth_client.get(WHOAMI).status_code == 200
    owner_session.execute(
        text("UPDATE shifde.sessions SET idle_expires_at = now() - interval '1 second'")
    )
    owner_session.commit()
    assert auth_client.get(WHOAMI).status_code == 401

    login(auth_client, ADMIN_A)
    owner_session.execute(
        text(
            "UPDATE shifde.sessions SET absolute_expires_at = now() - interval '1 second' "
            "WHERE revoked_at IS NULL AND idle_expires_at > now()"
        )
    )
    owner_session.commit()
    assert auth_client.get(WHOAMI).status_code == 401


def test_idle_window_slides_on_activity(
    auth_client: TestClient, admin_a: UUID, owner_session: Session
) -> None:
    login(auth_client, ADMIN_A)
    owner_session.execute(
        text(
            "UPDATE shifde.sessions SET last_seen_at = now() - interval '5 minutes', "
            "idle_expires_at = now() + interval '1 minute'"
        )
    )
    owner_session.commit()
    assert auth_client.get(WHOAMI).status_code == 200
    remaining = owner_session.execute(
        text("SELECT idle_expires_at - now() FROM shifde.sessions")
    ).scalar_one()
    assert remaining > timedelta(minutes=20)


def test_login_rotates_session_and_revokes_previous(
    auth_client: TestClient, admin_a: UUID, owner_session: Session
) -> None:
    login(auth_client, ADMIN_A)
    first = auth_client.cookies.get(COOKIE)
    login(auth_client, ADMIN_A)
    second = auth_client.cookies.get(COOKIE)
    assert first and second and first != second
    revoked = owner_session.execute(
        text("SELECT count(*) FROM shifde.sessions WHERE revoked_at IS NOT NULL")
    ).scalar_one()
    assert revoked == 1
    token_hashes = owner_session.execute(text("SELECT session_hash FROM shifde.sessions")).all()
    assert all(first.encode() not in bytes(h[0]) for h in token_hashes)  # only hashes stored


def test_csrf_required_on_mutations(auth_client: TestClient, admin_a: UUID) -> None:
    headers = login(auth_client, ADMIN_A)
    url = _probe_post(uuid7())
    missing = auth_client.delete(url)
    assert missing.status_code == 403 and missing.json()["code"] == "csrf_failed"
    wrong = auth_client.delete(url, headers={"X-CSRF-Token": "x" * 43})
    assert wrong.status_code == 403 and wrong.json()["code"] == "csrf_failed"
    ok = auth_client.delete(url, headers=headers)
    assert ok.status_code == 404  # past authentication and CSRF: the key does not exist
    assert auth_client.post("/api/auth/logout").status_code == 403  # logout needs CSRF too


def test_disallowed_origin_is_rejected_on_mutations(auth_client: TestClient, admin_a: UUID) -> None:
    r = auth_client.post(
        "/api/auth/local/login",
        json={"email": ADMIN_A, "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"


def test_cross_site_fetch_metadata_without_origin_is_rejected(
    auth_client: TestClient, admin_a: UUID
) -> None:
    creds = {"email": ADMIN_A, "password": PASSWORD}
    with TestClient(auth_client.app, base_url="https://testserver") as bare:  # sends NO Origin
        assert "origin" not in {k.lower() for k in bare.headers}
        r = bare.post("/api/auth/local/login", json=creds, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
        # same-origin / absent metadata (curl, server-to-server) is allowed
        ok = bare.post(
            "/api/auth/local/login", json=creds, headers={"Sec-Fetch-Site": "same-origin"}
        )
        assert ok.status_code == 200
        csrf = {"X-CSRF-Token": ok.json()["csrf_token"]}
        url = _probe_post(uuid7())
        r = bare.delete(url, headers={**csrf, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
        assert bare.delete(url, headers=csrf).status_code == 404


def test_local_auth_disabled_returns_auth_disabled(settings: Settings, admin_a: UUID) -> None:
    off = settings.model_copy(update={"auth_local_enabled": False})
    with client_for(off) as c:
        assert c.get("/api/auth/config").json() == {"local_enabled": False, "oidc_providers": []}
        r = c.post("/api/auth/local/login", json={"email": ADMIN_A, "password": "x"})
        assert r.status_code == 403 and r.json()["code"] == "auth_disabled"


def test_development_uses_plain_cookie_name(settings: Settings, admin_a: UUID) -> None:
    dev = settings.model_copy(update={"env": "development"})
    with client_for(dev) as c:
        c.base_url = c.base_url.copy_with(scheme="http")
        r = c.post(
            "/api/auth/local/login",
            json={"email": ADMIN_A, "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("shifde_session=") and "secure" not in cookie.lower()


def test_auth_config(auth_client: TestClient) -> None:
    r = auth_client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json() == {"local_enabled": True, "oidc_providers": []}


def test_session_and_logout_refuse_a_bearer_credential(auth_client: TestClient) -> None:
    """SETH `require_session_auth`: an API key has no browser session."""
    bearer = {"Authorization": "Bearer shf_test_abcdefgh_" + "x" * 43}
    assert auth_client.get("/api/auth/session", headers=bearer).status_code == 401
    assert auth_client.post("/api/auth/logout", headers=bearer).status_code == 401
