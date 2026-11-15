"""Service accounts and API keys through the admin routes (admin part 2, B-1601..B-1604).

The SETH 3ba0e06 tests that drive the admin key and service-account routes (carry_forward row 11
remainder, `test_p2_credentials.py`; `test_p2_review.py::test_personal_api_keys_can_only_be_rotated
_by_their_owner` and `::test_reenabling_a_role_user_or_service_account_requires_holding_its
_permissions`, rows 14 and 37), assertions unchanged. SETH's seeded `demo-a`/`demo-b` world is the
tenancy route world (`tests/tenancy/conftest.py`, `tests/tenancy/routes.py`): `A` and `B` are its
two clients, `ADMIN` is A's owner, `viewer@demo-a.local` A's viewer, `main` A's first workspace.
SETH's `new_user` (the administrator creates a person; B-913 gives that to the platform
administrator) becomes a person written as the schema owner whose roles A's owner binds through
`POST /role-bindings`; it ends signed in as that person, as SETH's redemption did. Audit queries
that read the whole table in SETH name A's client (other tests' rows may share the database).

Then the fixes of this part: id-only key mutators run only on a row loaded in the caller's client
(carry_forward row 87), and a key's creator is the caller (row 100, B-921).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.admin import credentials
from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity.passwords import PasswordService
from tests.admin.conftest import admin_client
from tests.identity.harness import PASSWORD
from tests.identity.harness import login as harness_login
from tests.tenancy.conftest import World, owner_member
from tests.tenancy.routes import People, ws_slug

KEY_RE = re.compile(r"^shf_test_[0-9A-Za-z]{8}_[0-9A-Za-z]{43}$")


@dataclass(frozen=True)
class Seth:
    """SETH's names for the route world."""

    A: str
    B: str
    ADMIN: str
    VIEWER: str
    MAIN: str  # slug of A's first workspace (SETH's `main`)
    tenant_a: UUID
    tenant_b: UUID


@pytest.fixture
def w(world: World, people: People) -> Seth:
    return Seth(
        A=f"/api/v1/orgs/{world.a.slug}",
        B=f"/api/v1/orgs/{world.b.slug}",
        ADMIN=people.owner_a,
        VIEWER=people.viewer_a,
        MAIN=ws_slug(world.a),
        tenant_a=world.a.tenant,
        tenant_b=world.b.tenant,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with admin_client(settings) as c:
        yield c


@pytest.fixture
def admin(client: TestClient, w: Seth) -> dict[str, str]:
    return login(client, w.ADMIN)


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    """SETH's `p2util.login`: a fresh sign-in, without presenting (and rotating) the previous
    person's session cookie."""
    client.cookies.clear()
    return harness_login(client, email, password)


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def post(client: TestClient, url: str, h: dict[str, str], body: dict[str, Any]) -> Any:
    r = client.post(url, json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def system_role(owner: Session, key: str) -> UUID:
    found: UUID = owner.execute(
        text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"), {"k": key}
    ).scalar_one()
    return found


def new_user(
    client: TestClient,
    h: dict[str, str],
    owner: Session,
    w: Seth,
    email: str,
    password: str,
    role_ids: list[str],
) -> str:
    """A member of A with a password and the given roles bound by A's owner at organization
    scope; ends signed in as that member (SETH's redemption opened the new member's session)."""
    uid = uuid7()
    owner.execute(
        text("INSERT INTO shifde.users (id, primary_email, display_name) VALUES (:i, :e, :e)"),
        {"i": uid, "e": email},
    )
    owner.execute(
        text("INSERT INTO shifde.local_credentials (user_id, password_hash) VALUES (:u, :h)"),
        {"u": uid, "h": PasswordService(fast=True).hash(password)},
    )
    owner_member(owner, w.tenant_a, uid)
    owner.commit()
    for role_id in role_ids:
        bound = client.post(
            f"{w.A}/role-bindings",
            headers=h,
            json={
                "subject_type": "user",
                "subject_id": str(uid),
                "role_id": str(role_id),
                "scope": {"type": "tenant", "id": str(w.tenant_a)},
            },
        )
        assert bound.status_code == 201, bound.text
    login(client, email, password)
    return str(uid)


def make_key(
    client: TestClient,
    h: dict[str, str],
    owner: Session,
    w: Seth,
    *,
    role: str = "tenant_administrator",
    scopes: list[str] | None = None,
    name: str = "ci",
    resource_scope: dict[str, list[str]] | None = None,
    days: int = 30,
) -> tuple[dict[str, Any], dict[str, Any]]:
    sa = post(client, f"{w.A}/service-accounts", h, {"name": name})
    bound = client.post(
        f"{w.A}/role-bindings",
        headers=h,
        json={
            "subject_type": "service_account",
            "subject_id": sa["id"],
            "role_id": str(system_role(owner, role)),
            "scope": {"type": "tenant", "id": str(w.tenant_a)},
        },
    )
    assert bound.status_code == 201, bound.text
    body: dict[str, Any] = {
        "name": name,
        "principal_type": "service_account",
        "principal_id": sa["id"],
        "scopes": scopes or ["workspace:read", "project:read"],
        "expires_in_days": days,
    }
    if resource_scope:
        body["resource_scope"] = resource_scope
    return sa, post(client, f"{w.A}/api-keys", h, body)


# ------------------------------------------------------------------ SETH test_p2_credentials
def test_bearer_mutation_without_csrf_succeeds_when_granted(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    _, created = make_key(
        client, admin, owner_session, w, scopes=["workspace:create", "workspace:read"]
    )
    client.cookies.clear()
    r = client.post(
        f"{w.A}/workspaces",
        json={"slug": "viaapi", "name": "Via API"},
        headers=bearer(created["secret"]),
    )
    assert r.status_code == 201, r.text
    actor = owner_session.execute(
        text(
            "SELECT actor_type, auth_method FROM shifde.audit_events "
            "WHERE action = 'tenancy.workspace.created' AND tenant_id = :t"
        ),
        {"t": w.tenant_a},
    ).one()
    assert tuple(actor) == ("service_account", "api_key")


def test_database_refuses_to_unrevoke_or_extend_a_key(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    _, created = make_key(client, admin, owner_session, w)
    kid = created["api_key"]["id"]
    with pytest.raises(Exception, match="expiry can only be shortened"):
        owner_session.execute(
            text(
                "UPDATE shifde.api_keys SET expires_at = expires_at + interval '1 day' WHERE id = :k"
            ),
            {"k": kid},
        )
    owner_session.rollback()
    owner_session.execute(
        text("UPDATE shifde.api_keys SET revoked_at = now() WHERE id = :k"), {"k": kid}
    )
    owner_session.commit()
    with pytest.raises(Exception, match="revoked API key cannot be changed"):
        owner_session.execute(
            text("UPDATE shifde.api_keys SET revoked_at = NULL WHERE id = :k"), {"k": kid}
        )
    owner_session.rollback()


def test_rotation_with_grace_then_old_key_expires(
    client: TestClient, owner_session: Session, w: Seth
) -> None:
    admin = login(client, w.ADMIN)
    _, created = make_key(client, admin, owner_session, w)
    old_id, old_secret = created["api_key"]["id"], created["secret"]
    rot = client.post(f"{w.A}/api-keys/{old_id}/rotate", json={"grace_hours": 2}, headers=admin)
    assert rot.status_code == 201, rot.text
    new = rot.json()
    assert new["secret"] != old_secret and KEY_RE.match(new["secret"])
    assert new["api_key"]["rotated_from_id"] == old_id
    assert new["api_key"]["scopes"] == created["api_key"]["scopes"]
    # both work during the grace window; old key's expiry is now ~ +2h
    assert client.get(f"{w.A}/workspaces", headers=bearer(old_secret)).status_code == 200
    assert client.get(f"{w.A}/workspaces", headers=bearer(new["secret"])).status_code == 200
    old_exp = owner_session.execute(
        text("SELECT expires_at FROM shifde.api_keys WHERE id = :k"), {"k": old_id}
    ).scalar_one()
    assert timedelta(hours=1, minutes=55) < old_exp - utcnow() < timedelta(hours=2, minutes=5)
    # rotate with no grace: the (new) key expires immediately
    admin = login(client, w.ADMIN)
    again = client.post(
        f"{w.A}/api-keys/{new['api_key']['id']}/rotate", json={"grace_hours": 0}, headers=admin
    )
    assert again.status_code == 201
    r = client.get(f"{w.A}/workspaces", headers=bearer(new["secret"]))
    assert r.status_code == 401 and r.json()["code"] == "credential_expired"
    # a revoked/expired key cannot be rotated
    assert (
        client.post(
            f"{w.A}/api-keys/{new['api_key']['id']}/rotate", json={}, headers=admin
        ).status_code
        == 409
    )


def test_key_scopes_mask_the_principal_grants(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    # SA holds Tenant Administrator, the key only workspace:read -> effective = intersection
    _, created = make_key(client, admin, owner_session, w, scopes=["workspace:read"])
    h = bearer(created["secret"])
    client.cookies.clear()
    assert client.get(f"{w.A}/workspaces", headers=h).status_code == 200
    assert (
        client.post(f"{w.A}/workspaces", json={"slug": "n", "name": "n"}, headers=h).status_code
        == 403
    )
    perms = client.get(f"{w.A}/me/permissions", headers=h).json()["permissions"]
    assert perms == ["workspace:read"]
    assert client.get(f"{w.A}/roles", headers=h).status_code == 403  # role:read not in scopes


def test_resource_scope_restricts_to_listed_workspaces(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    w1 = post(client, f"{w.A}/workspaces", admin, {"slug": "w1", "name": "W1"})
    post(client, f"{w.A}/workspaces", admin, {"slug": "w2", "name": "W2"})
    _, created = make_key(
        client,
        admin,
        owner_session,
        w,
        scopes=["workspace:read", "project:read"],
        resource_scope={"workspace_ids": [w1["id"]]},
    )
    client.cookies.clear()
    h = bearer(created["secret"])
    listing = client.get(f"{w.A}/workspaces", headers=h).json()["items"]
    assert [ws["slug"] for ws in listing] == ["w1"]
    assert client.get(f"{w.A}/workspaces/w1", headers=h).status_code == 200
    assert client.get(f"{w.A}/workspaces/w2", headers=h).status_code == 404
    assert client.get(f"{w.A}/workspaces/{w.MAIN}", headers=h).status_code == 404


def test_key_scopes_cannot_exceed_what_the_creator_holds(
    client: TestClient, owner_session: Session, w: Seth
) -> None:
    admin = login(client, w.ADMIN)
    role = post(
        client,
        f"{w.A}/roles",
        admin,
        {
            "key": "keymgr",
            "name": "keymgr",
            "permissions": [
                "apikey:manage",
                "apikey:read",
                "serviceaccount:manage",
                "serviceaccount:read",
                "workspace:read",
            ],
        },
    )["id"]
    tag = uuid7().hex[-12:]
    keymgr = f"keymgr-{tag}@example.com"
    new_user(client, admin, owner_session, w, keymgr, "a-long-password-1", [role])
    kh = login(client, keymgr, "a-long-password-1")
    sa = post(client, f"{w.A}/service-accounts", kh, {"name": "sa2"})
    base = {
        "name": "k",
        "principal_type": "service_account",
        "principal_id": sa["id"],
        "expires_in_days": 5,
    }
    for scopes in (["secret:manage"], ["workspace:read", "tenant:admin"], ["agent:execute"]):
        r = client.post(f"{w.A}/api-keys", json={**base, "scopes": scopes}, headers=kh)
        assert r.status_code == 403, (scopes, r.text)
    assert (
        client.post(
            f"{w.A}/api-keys", json={**base, "scopes": ["platform:admin"]}, headers=kh
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{w.A}/api-keys", json={**base, "scopes": ["nope:nope"]}, headers=kh
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{w.A}/api-keys", json={**base, "scopes": ["workspace:read"]}, headers=kh
        ).status_code
        == 201
    )
    # expiry is mandatory and capped
    over = client.post(
        f"{w.A}/api-keys",
        json={**base, "scopes": ["workspace:read"], "expires_in_days": 366},
        headers=kh,
    )
    assert over.status_code == 422
    assert (
        client.post(
            f"{w.A}/api-keys",
            json={**base, "scopes": ["workspace:read"]} | {"expires_in_days": 0},
            headers=kh,
        ).status_code
        == 422
    )
    missing = {k: v for k, v in base.items() if k != "expires_in_days"} | {
        "scopes": ["workspace:read"]
    }
    assert client.post(f"{w.A}/api-keys", json=missing, headers=kh).status_code == 422
    # rotation re-applies the rule: a lesser manager cannot rotate a key with scopes it lacks
    admin = login(client, w.ADMIN)
    _, strong = make_key(
        client, admin, owner_session, w, scopes=["secret:manage", "workspace:read"], name="strong"
    )
    kh = login(client, keymgr, "a-long-password-1")
    assert (
        client.post(
            f"{w.A}/api-keys/{strong['api_key']['id']}/rotate", json={}, headers=kh
        ).status_code
        == 403
    )


def test_privileged_scope_needs_a_creator_that_holds_it_and_key_can_use_it(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    _, created = make_key(client, admin, owner_session, w, scopes=["role:read", "role:manage"])
    client.cookies.clear()
    r = client.get(f"{w.A}/roles", headers=bearer(created["secret"]))
    assert r.status_code == 200
    n = owner_session.execute(
        text(
            "SELECT count(*) FROM shifde.policy_decisions WHERE principal_type = 'service_account'"
        )
    ).scalar_one()
    assert n == 0  # a read does not exercise the privileged permission, so nothing is logged


def test_personal_keys_only_for_oneself(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    viewer = next(
        u
        for u in client.get(f"{w.A}/users", headers=admin).json()["items"]
        if u["user"]["email"] == w.VIEWER
    )
    r = client.post(
        f"{w.A}/api-keys",
        headers=admin,
        json={
            "name": "p",
            "principal_type": "user",
            "principal_id": viewer["user"]["id"],
            "scopes": ["workspace:read"],
            "expires_in_days": 3,
        },
    )
    assert r.status_code == 403
    me = next(
        u
        for u in client.get(f"{w.A}/users", headers=admin).json()["items"]
        if u["user"]["email"] == w.ADMIN
    )
    ok = client.post(
        f"{w.A}/api-keys",
        headers=admin,
        json={
            "name": "p",
            "principal_type": "user",
            "principal_id": me["user"]["id"],
            "scopes": ["workspace:read"],
            "expires_in_days": 3,
        },
    )
    assert ok.status_code == 201
    client.cookies.clear()
    assert client.get(f"{w.A}/workspaces", headers=bearer(ok.json()["secret"])).status_code == 200


def test_suspending_a_user_revokes_sessions_and_keys_immediately(
    client: TestClient, owner_session: Session, w: Seth
) -> None:
    admin = login(client, w.ADMIN)
    role = post(
        client,
        f"{w.A}/roles",
        admin,
        {
            "key": "dev",
            "name": "dev",
            "permissions": ["workspace:read", "apikey:manage", "apikey:read"],
        },
    )["id"]
    susp_email = f"susp-{uuid7().hex[-12:]}@example.com"
    uid = new_user(client, admin, owner_session, w, susp_email, "a-long-password-1", [role])
    uh = login(client, susp_email, "a-long-password-1")
    # B-1014: suspension in a client revokes the sessions pinned to it (an unpinned one loses the
    # client through B-1009); SETH's session was the client's, so the person's are pinned to A
    owner_session.execute(
        text("UPDATE shifde.sessions SET active_tenant_id = :t WHERE user_id = :u"),
        {"t": w.tenant_a, "u": UUID(uid)},
    )
    owner_session.commit()
    key = post(
        client,
        f"{w.A}/api-keys",
        uh,
        {
            "name": "mine",
            "principal_type": "user",
            "principal_id": uid,
            "scopes": ["workspace:read"],
            "expires_in_days": 5,
        },
    )
    assert client.get(f"{w.A}/workspaces", headers=bearer(key["secret"])).status_code == 200
    assert client.get(f"{w.A}/workspaces", headers=uh).status_code == 200  # session works
    session_cookie = dict(client.cookies)
    admin = login(client, w.ADMIN)
    row = next(
        u
        for u in client.get(f"{w.A}/users", headers=admin).json()["items"]
        if u["user"]["id"] == uid
    )
    r = client.patch(
        f"{w.A}/users/{uid}",
        json={"status": "suspended", "row_version": row["row_version"]},
        headers=admin,
    )
    assert r.status_code == 200
    audit = owner_session.execute(
        text(
            "SELECT details FROM shifde.audit_events "
            "WHERE action = 'identity.user.suspended' AND tenant_id = :t"
        ),
        {"t": w.tenant_a},
    ).scalar_one()
    assert audit["sessions_revoked"] >= 1 and audit["keys_revoked"] == 1
    client.cookies.clear()
    gone = client.get(f"{w.A}/workspaces", headers=bearer(key["secret"]))
    assert gone.status_code == 401 and gone.json()["code"] == "credential_revoked"
    client.cookies.update(session_cookie)
    assert client.get("/api/auth/session").status_code == 401  # the session is revoked too


def test_disabling_a_service_account_revokes_its_keys(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    sa, created = make_key(client, admin, owner_session, w)
    r = client.patch(
        f"{w.A}/service-accounts/{sa['id']}",
        json={"status": "disabled", "row_version": sa["row_version"]},
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    client.cookies.clear()
    gone = client.get(f"{w.A}/workspaces", headers=bearer(created["secret"]))
    assert gone.status_code == 401 and gone.json()["code"] == "credential_revoked"


def test_service_account_crud(
    client: TestClient, admin: dict[str, str], owner_session: Session, world: World, w: Seth
) -> None:
    # SETH's demo-a has no service account; the route world's is removed first
    owner_session.execute(
        text("DELETE FROM shifde.service_accounts WHERE id = :i"), {"i": world.a.service_account}
    )
    owner_session.commit()
    sa = post(client, f"{w.A}/service-accounts", admin, {"name": "etl", "description": "d"})
    assert sa["status"] == "active" and sa["row_version"] == 1
    assert (
        client.post(f"{w.A}/service-accounts", json={"name": "etl"}, headers=admin).status_code
        == 409
    )
    got = client.get(f"{w.A}/service-accounts/{sa['id']}", headers=admin)
    assert got.status_code == 200 and got.json()["name"] == "etl"
    assert [
        i["name"] for i in client.get(f"{w.A}/service-accounts", headers=admin).json()["items"]
    ] == ["etl"]
    stale = client.patch(
        f"{w.A}/service-accounts/{sa['id']}", json={"name": "z", "row_version": 7}, headers=admin
    )
    assert stale.status_code == 409
    assert client.get(f"{w.B}/service-accounts", headers=admin).status_code == 404  # other tenant


def test_key_of_one_org_cannot_address_another_org_and_has_no_browser_session(
    client: TestClient, admin: dict[str, str], owner_session: Session, w: Seth
) -> None:
    _, created = make_key(client, admin, owner_session, w)
    client.cookies.clear()
    h = bearer(created["secret"])
    assert client.get(f"{w.B}/workspaces", headers=h).status_code == 404
    assert client.get("/api/auth/session", headers=h).status_code == 401
    assert client.post("/api/auth/logout", headers=h).status_code == 401
    assert client.get("/api/v1/orgs", headers=h).json()["items"] == []  # SA has no memberships


# ------------------------------------------------------------------ SETH test_p2_review
def test_personal_api_keys_can_only_be_rotated_by_their_owner(
    client: TestClient, owner_session: Session, w: Seth
) -> None:
    admin = login(client, w.ADMIN)
    role = post(
        client,
        f"{w.A}/roles",
        admin,
        {"key": "keyer", "name": "keyer", "permissions": ["apikey:manage", "workspace:read"]},
    )
    victim = f"victim-{uuid7().hex[-12:]}@demo-a.local"
    victim_id = new_user(
        client, admin, owner_session, w, victim, "victim-password-123456", [role["id"]]
    )
    vh = {"X-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]}
    k = post(
        client,
        f"{w.A}/api-keys",
        vh,
        {
            "name": "mine",
            "principal_type": "user",
            "principal_id": victim_id,
            "scopes": ["workspace:read"],
            "expires_in_days": 5,
        },
    )
    admin = login(client, w.ADMIN)
    denied = client.post(
        f"{w.A}/api-keys/{k['api_key']['id']}/rotate", json={"grace_hours": 0}, headers=admin
    )
    assert denied.status_code == 403 and "secret" not in denied.text
    # others with apikey:manage may still revoke it
    revoked = client.post(f"{w.A}/api-keys/{k['api_key']['id']}/revoke", headers=admin)
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    # the owner may rotate their own (active) key
    vh = {"X-CSRF-Token": login(client, victim, "victim-password-123456")["X-CSRF-Token"]}
    k2 = post(
        client,
        f"{w.A}/api-keys",
        vh,
        {
            "name": "again",
            "principal_type": "user",
            "principal_id": victim_id,
            "scopes": ["workspace:read"],
            "expires_in_days": 5,
        },
    )
    own = client.post(
        f"{w.A}/api-keys/{k2['api_key']['id']}/rotate", json={"grace_hours": 1}, headers=vh
    )
    assert own.status_code == 201 and own.json()["api_key"]["principal_id"] == victim_id


def test_reenabling_a_role_user_or_service_account_requires_holding_its_permissions(
    client: TestClient, owner_session: Session, w: Seth
) -> None:
    admin = login(client, w.ADMIN)
    tid = str(w.tenant_a)
    big = post(
        client,
        f"{w.A}/roles",
        admin,
        {"key": "big", "name": "big", "permissions": ["audit:read", "workspace:read"]},
    )
    off = client.patch(
        f"{w.A}/roles/{big['id']}",
        json={"status": "disabled", "row_version": big["row_version"]},
        headers=admin,
    )
    assert off.status_code == 200 and off.json()["status"] == "disabled"
    sa = post(client, f"{w.A}/service-accounts", admin, {"name": "bot"})
    bound = client.post(
        f"{w.A}/role-bindings",
        headers=admin,
        json={
            "subject_type": "service_account",
            "subject_id": sa["id"],
            "role_id": str(system_role(owner_session, "tenant_administrator")),
            "scope": {"type": "tenant", "id": tid},
        },
    )
    assert bound.status_code == 201
    off_sa = client.patch(
        f"{w.A}/service-accounts/{sa['id']}",
        json={"status": "disabled", "row_version": 1},
        headers=admin,
    )
    assert off_sa.status_code == 200
    delegate_role = post(
        client,
        f"{w.A}/roles",
        admin,
        {
            "key": "dlg",
            "name": "dlg",
            "permissions": [
                "role:manage",
                "role:read",
                "serviceaccount:manage",
                "serviceaccount:read",
                "member:manage",
                "member:read",
                "workspace:read",
            ],
        },
    )["id"]
    dlg = f"dlg-{uuid7().hex[-12:]}@example.com"
    new_user(client, admin, owner_session, w, dlg, "a-long-password-1", [delegate_role])
    dh = login(client, dlg, "a-long-password-1")
    assert (
        client.patch(f"{w.A}/roles/{big['id']}", json={"status": "active"}, headers=dh).status_code
        == 403
    )
    assert (
        client.patch(
            f"{w.A}/service-accounts/{sa['id']}",
            json={"status": "active", "row_version": off_sa.json()["row_version"]},
            headers=dh,
        ).status_code
        == 403
    )
    admin = login(client, w.ADMIN)
    assert (
        client.patch(
            f"{w.A}/roles/{big['id']}", json={"status": "active"}, headers=admin
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"{w.A}/service-accounts/{sa['id']}",
            json={"status": "active", "row_version": off_sa.json()["row_version"]},
            headers=admin,
        ).status_code
        == 200
    )
    # a suspended tenant admin cannot be reactivated by a lesser manager either
    users = client.get(f"{w.A}/users", headers=admin).json()["items"]
    tenant_admin = next(u for u in users if u["user"]["email"] == w.ADMIN)
    ta2 = f"ta2-{uuid7().hex[-12:]}@example.com"
    uid = new_user(
        client,
        admin,
        owner_session,
        w,
        ta2,
        "a-long-password-1",
        [str(system_role(owner_session, "tenant_administrator"))],
    )
    admin = login(client, w.ADMIN)
    other = next(
        u
        for u in client.get(f"{w.A}/users", headers=admin).json()["items"]
        if u["user"]["id"] == uid
    )
    susp = client.patch(
        f"{w.A}/users/{uid}",
        json={"status": "suspended", "row_version": other["row_version"]},
        headers=admin,
    )
    assert susp.status_code == 200
    dh = login(client, dlg, "a-long-password-1")
    back = client.patch(
        f"{w.A}/users/{uid}",
        json={"status": "active", "row_version": susp.json()["row_version"]},
        headers=dh,
    )
    assert back.status_code == 403
    _ = tenant_admin


# ------------------------------------------------------------------ fixes of this part
def test_key_mutators_run_only_on_a_key_loaded_in_the_callers_client(
    client: TestClient,
    admin: dict[str, str],
    owner_session: Session,
    world: World,
    people: People,
    w: Seth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """carry_forward row 87: `revoke_api_key` and `shorten_api_key_expiry` filter by id only;
    the routes reach them only with a key read in the caller's client, so B's key id under A's
    org never gets to them and B's key is unchanged. (`touch_api_key` is called by bearer
    authentication only, for the key just verified; admin does not call it.)"""
    seen: list[UUID] = []
    real_revoke, real_shorten = (
        credentials.identity.revoke_api_key,
        (credentials.identity.shorten_api_key_expiry),
    )

    def revoke(session: Session, key_id: UUID, *args: Any) -> bool:
        seen.append(key_id)
        return real_revoke(session, key_id, *args)

    def shorten(session: Session, key_id: UUID, *args: Any) -> None:
        seen.append(key_id)
        real_shorten(session, key_id, *args)

    monkeypatch.setattr(credentials.identity, "revoke_api_key", revoke)
    monkeypatch.setattr(credentials.identity, "shorten_api_key_expiry", shorten)
    hb = login(client, people.owner_b)
    b_key = post(
        client,
        f"{w.B}/api-keys",
        hb,
        {
            "name": "b",
            "principal_type": "service_account",
            "principal_id": str(world.b.service_account),
            "scopes": ["workspace:read"],
            "expires_in_days": 5,
        },
    )["api_key"]
    admin = login(client, w.ADMIN)
    for action, body in (("revoke", None), ("rotate", {"grace_hours": 0})):
        r = client.post(f"{w.A}/api-keys/{b_key['id']}/{action}", json=body, headers=admin)
        assert r.status_code == 404, r.text
    assert seen == []
    row = owner_session.execute(
        text("SELECT revoked_at, expires_at FROM shifde.api_keys WHERE id = :k"),
        {"k": b_key["id"]},
    ).one()
    assert row.revoked_at is None and row.expires_at.isoformat().startswith(
        b_key["expires_at"][:19]
    )
    _, own = make_key(client, admin, owner_session, w)
    own_id = UUID(own["api_key"]["id"])
    assert client.post(f"{w.A}/api-keys/{own_id}/rotate", json={}, headers=admin).status_code == 201
    assert client.post(f"{w.A}/api-keys/{own_id}/revoke", headers=admin).status_code == 200
    assert seen == [own_id, own_id]


def test_a_key_names_the_caller_as_its_creator(
    client: TestClient, admin: dict[str, str], owner_session: Session, world: World, w: Seth
) -> None:
    """carry_forward row 100 (B-921): the route passes the caller's own id as `created_by`, for
    a personal key, a service-account key and a rotation; the body cannot name a creator."""
    me = owner_session.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": w.ADMIN}
    ).scalar_one()
    base = {"scopes": ["workspace:read"], "expires_in_days": 3}
    personal = post(
        client,
        f"{w.A}/api-keys",
        admin,
        {**base, "name": "p", "principal_type": "user", "principal_id": str(me)},
    )["api_key"]
    machine = post(
        client,
        f"{w.A}/api-keys",
        admin,
        {
            **base,
            "name": "m",
            "principal_type": "service_account",
            "principal_id": str(world.a.service_account),
        },
    )["api_key"]
    rotated = post(client, f"{w.A}/api-keys/{machine['id']}/rotate", admin, {})["api_key"]
    creators = dict(
        owner_session.execute(
            text("SELECT id, created_by FROM shifde.api_keys WHERE id = ANY(:ids)"),
            {"ids": [UUID(personal["id"]), UUID(machine["id"]), UUID(rotated["id"])]},
        ).all()
    )
    assert set(creators.values()) == {me}
    smuggled = client.post(
        f"{w.A}/api-keys",
        json={
            **base,
            "name": "x",
            "principal_type": "service_account",
            "principal_id": str(world.a.service_account),
            "created_by": str(world.a.member),
        },
        headers=admin,
    )
    assert smuggled.status_code == 422
