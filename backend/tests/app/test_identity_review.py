"""Session revocation and pinning (SETH 3ba0e06 `test_p2_review.py`, tests 9 and the pinned-session pair)."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests._support.fullapp import World
from tests._support.p2util import login, new_user, tenant_id
from tests._support.tenants import bind_user

A = "/api/v1/orgs/demo-a"
B = "/api/v1/orgs/demo-b"


def _token(i: int) -> str:
    return f"suspension-session-token-{i}-xxxxxxxxxxxxxxxxxxxxxxxx"


def _hash(i: int) -> bytes:
    """Session hash of session `i`: a real token's hash for the unpinned session (0), so the test
    can send its cookie; arbitrary bytes for the pinned ones."""
    return hashlib.sha256(_token(i).encode()).digest() if i == 0 else bytes([i]) * 32


# ---------------------------------------------------------------- 9: suspension revokes unbound + same-tenant sessions
def test_suspension_revokes_unbound_and_same_tenant_sessions_but_not_other_tenant_pins(
    client: TestClient, world: World, owner_session: Session
) -> None:
    admin = login(client, world.passwords, "admin@demo-a.local")
    uid = new_user(client, admin, "demo-a", "multi@example.com", "a-long-password-1")
    a, b = tenant_id(owner_session, "demo-a"), tenant_id(owner_session, "demo-b")
    owner_session.execute(
        text(
            "INSERT INTO shifde.memberships (id, tenant_id, user_id) VALUES (gen_random_uuid(), :t, :u)"
        ),
        {"t": b, "u": uid},
    )
    bind_user(owner_session, b, uid)  # a live binding in B, so B stays visible (B-1009)
    for i, pin in enumerate((None, a, b)):
        owner_session.execute(
            text(
                "INSERT INTO shifde.sessions (id, session_hash, user_id, active_tenant_id, csrf_hash, idle_expires_at, absolute_expires_at) VALUES (gen_random_uuid(), :h, :u, :p, :c, now() + interval '1 hour', now() + interval '1 hour')"
            ),
            {"h": _hash(i), "u": uid, "p": pin, "c": b"c" * 32},
        )
    owner_session.commit()
    admin = login(client, world.passwords, "admin@demo-a.local")
    row = next(
        u for u in client.get(f"{A}/users", headers=admin).json()["items"] if u["user"]["id"] == uid
    )
    assert (
        client.patch(
            f"{A}/users/{uid}",
            json={"status": "suspended", "row_version": row["row_version"]},
            headers=admin,
        ).status_code
        == 200
    )
    state = dict(
        owner_session.execute(
            text(
                "SELECT active_tenant_id::text, revoked_at IS NOT NULL FROM shifde.sessions WHERE user_id = :u AND session_hash = ANY(:h)"
            ),
            {"u": uid, "h": [_hash(i) for i in range(3)]},
        ).all()
    )
    # B-1014: SETH also revoked the unpinned session; the unified rule revokes only sessions
    # pinned to the suspending client, and an unpinned session loses that client through the
    # membership visibility rule (pinned: tests/tenancy/test_service.py::
    # test_suspending_in_a_revokes_only_sessions_pinned_to_a_and_never_an_admin; database level:
    # tests/tenancy/test_memberships.py::test_an_inactive_membership_hides_the_client)
    assert state == {None: False, str(a): True, str(b): False}
    # through the app: the live unpinned session no longer reaches A, and still reaches B
    client.cookies.clear()
    cookie = {"Cookie": f"__Host-shifde_session={_token(0)}"}
    assert client.get(f"{A}/me/permissions", headers=cookie).status_code == 404
    assert client.get(f"{B}/me/permissions", headers=cookie).status_code == 200


def test_pinned_sessions_never_carry_platform_admin_and_promotion_revokes_sessions(
    client: TestClient, world: World, owner_session: Session
) -> None:
    uid = owner_session.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = 'viewer@demo-a.local'")
    ).scalar_one()
    a = tenant_id(owner_session, "demo-a")
    owner_session.execute(
        text("UPDATE shifde.users SET is_platform_admin = true WHERE id = :u"), {"u": uid}
    )
    owner_session.commit()
    for i, pin in enumerate((None, a)):
        tok = f"pinned-session-token-{i}-xxxxxxxxxxxxxxxxxxxxxxxx"
        csrf = f"csrf-{i}"
        owner_session.execute(
            text(
                "INSERT INTO shifde.sessions (id, session_hash, user_id, active_tenant_id, csrf_hash, idle_expires_at, absolute_expires_at) VALUES (gen_random_uuid(), :h, :u, :p, :c, now() + interval '1 hour', now() + interval '1 hour')"
            ),
            {
                "h": hashlib.sha256(tok.encode()).digest(),
                "u": uid,
                "p": pin,
                "c": hashlib.sha256(csrf.encode()).digest(),
            },
        )
    owner_session.commit()
    flags = {}
    for i, pin in enumerate((None, a)):
        client.cookies.clear()
        cookie = f"__Host-shifde_session=pinned-session-token-{i}-xxxxxxxxxxxxxxxxxxxxxxxx"
        r = client.get("/api/auth/session", headers={"Cookie": cookie})
        assert r.status_code == 200, (i, r.text)
        flags[pin] = r.json()["user"]["is_platform_admin"]
    assert flags == {None: True, a: False}  # an unpinned session keeps it; a pinned one never does
    # promotion revokes every session of the user (here: promote a second user with a session)
    other = owner_session.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = 'admin@demo-b.local'")
    ).scalar_one()
    owner_session.execute(
        text(
            "INSERT INTO shifde.sessions (id, session_hash, user_id, csrf_hash, idle_expires_at, absolute_expires_at) VALUES (gen_random_uuid(), :h, :u, :c, now() + interval '1 hour', now() + interval '1 hour')"
        ),
        {"h": b"p" * 32, "u": other, "c": b"c" * 32},
    )
    owner_session.commit()
    owner_session.execute(
        text("UPDATE shifde.users SET is_platform_admin = true WHERE id = :u"), {"u": other}
    )
    owner_session.commit()
    assert (
        owner_session.execute(
            text("SELECT revoked_at IS NOT NULL FROM shifde.sessions WHERE session_hash = :h"),
            {"h": b"p" * 32},
        ).scalar_one()
        is True
    )
