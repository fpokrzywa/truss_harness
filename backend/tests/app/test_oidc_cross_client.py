"""Cross-client tests of the mounted OpenID Connect routes and the installation IdP secret path
(B-2260..B-2265): through the full app as `shifde_app`, and on the database as `shifde_app` and as
the schema owner (whose BYPASSRLS shows the definer's own checks, not row-level security, decide).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any  # Any: httpx responses
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.fake_oidc import FakeOidcProvider
from tests._support.fullapp import World, login
from tests._support.tenants import bind_user
from tests.app.oidc_helpers import (
    FAIL,
    bind_group,
    create_idp,
    events,
    finish,
    link,
    new_person,
    oidc_app,
    ok_login,
    platform_group,
    start,
    tenant_id,
)

DEFINER = "SELECT secret_id FROM shifde.shifde_installation_idp_secret(:i)"


@pytest.fixture
def provider() -> FakeOidcProvider:
    return FakeOidcProvider()


@pytest.fixture
def env(settings: Settings, world: World, provider: FakeOidcProvider) -> Iterator[TestClient]:
    with oidc_app(settings, provider) as c:
        yield c


def _member(owner: Session, tenant: UUID, user: UUID) -> None:
    owner.execute(
        text("INSERT INTO shifde.memberships (id, tenant_id, user_id) VALUES (:i, :t, :u)"),
        {"i": uuid7(), "t": tenant, "u": user},
    )
    bind_user(owner, tenant, user)
    owner.commit()


def _secret_of(owner: Session, idp: str) -> tuple[UUID, UUID | None]:
    row = owner.execute(
        text(
            "SELECT s.id, s.tenant_id FROM shifde.idp_configs c JOIN shifde.secrets s "
            "ON s.id = c.client_secret_ref_id WHERE c.id = :i"
        ),
        {"i": idp},
    ).one()
    owner.rollback()
    return row[0], row[1]


def _same_failure(r: Any, reference: Any) -> None:
    """A refusal that answers exactly like an unknown IdP id: status, location, cookies."""
    assert r.status_code == reference.status_code == 302
    assert r.headers["location"] == reference.headers["location"] == FAIL
    assert r.headers.get("set-cookie") == reference.headers.get("set-cookie")


def test_bs_idp_never_signs_a_person_into_a(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    """B's IdP signs in a member of A and B pinned to B only (A's routes answer like a missing
    organization); A's member who is not B's is refused with the generic failure; B's secret is
    read in B's context and filed in B alone."""
    a, b = tenant_id(owner_session, "demo-a"), tenant_id(owner_session, "demo-b")
    b_idp = create_idp(env, world, tenant="demo-b")
    both = new_person(owner_session, "both@example.com")
    _member(owner_session, a, both)
    _member(owner_session, b, both)
    link(owner_session, both, b_idp, "both")
    r = ok_login(env, provider, b_idp, sub="both", email="both@example.com")
    assert r.status_code == 302 and r.headers["location"] == "/", r.headers
    session = env.get("/api/auth/session").json()
    assert [m["org"]["slug"] for m in session["memberships"]] == ["demo-b"]
    assert env.get("/api/v1/orgs/demo-b").status_code == 200
    assert env.get("/api/v1/orgs/demo-a").status_code == 404
    assert env.get("/api/v1/orgs/demo-a/me/permissions").status_code == 404
    assert [o["slug"] for o in env.get("/api/v1/orgs").json()["items"]] == ["demo-b"]
    env.cookies.clear()
    only_a = new_person(owner_session, "only-a@example.com")
    _member(owner_session, a, only_a)
    link(owner_session, only_a, b_idp, "only-a")
    r = ok_login(env, provider, b_idp, code="c2", sub="only-a", email="only-a@example.com")
    assert r.status_code == 302 and r.headers["location"] == FAIL
    assert env.get("/api/auth/session").status_code == 401
    secret, owner_of = _secret_of(owner_session, b_idp)
    assert owner_of == b
    log = owner_session.execute(
        text("SELECT tenant_id FROM shifde.secret_access_log WHERE secret_id = :s"), {"s": secret}
    ).scalars()
    assert set(log) == {b}
    owner_session.rollback()
    assert len(events(owner_session, "secret.resolved", b)) == 2
    assert events(owner_session, "secret.resolved", a) == []
    assert events(owner_session, "secret.idp_client_secret.resolved", None) == []


def test_bs_idp_id_answers_like_an_unknown_id_on_login_and_callback(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    """A state started at A's IdP does not complete at B's IdP: the callback answers exactly like
    one for an unknown id, as does B's disabled IdP at login and callback; nothing signs in. B's
    active IdP starts a flow filed in B, never in A (identity review 4 item 7: a client IdP's
    login URL is shareable)."""
    a, b = tenant_id(owner_session, "demo-a"), tenant_id(owner_session, "demo-b")
    a_idp = create_idp(env, world, tenant="demo-a")
    b_idp = create_idp(env, world, tenant="demo-b")
    b_off = create_idp(env, world, tenant="demo-b", issuer="https://idp.test/off")
    owner_session.execute(
        text("UPDATE shifde.idp_configs SET status = 'disabled' WHERE id = :i"), {"i": b_off}
    )
    owner_session.commit()
    unknown = str(uuid7())
    missing = env.get(f"/api/auth/oidc/{unknown}/login", follow_redirects=False)
    _same_failure(env.get(f"/api/auth/oidc/{b_off}/login", follow_redirects=False), missing)
    auth = start(env, provider, a_idp)
    provider.issue_code(auth, provider.claims_for(auth))
    at_b = finish(env, b_idp, "c1", auth.state)  # A's binding cookie is still presented here
    used = owner_session.execute(
        text("SELECT used_at IS NOT NULL FROM shifde.oidc_auth_requests WHERE idp_config_id = :i"),
        {"i": a_idp},
    ).scalar_one()
    owner_session.rollback()
    assert used is False  # B's callback did not consume A's flow
    missing_cb = finish(env, unknown, "c1", auth.state)
    _same_failure(missing_cb, missing)
    _same_failure(at_b, missing_cb)
    _same_failure(finish(env, b_off, "c1", auth.state), missing_cb)
    assert env.get("/api/auth/session").status_code == 401
    start(env, provider, b_idp)
    rows = owner_session.execute(
        text("SELECT idp_config_id::text, tenant_id FROM shifde.oidc_auth_requests")
    ).all()
    owner_session.rollback()
    assert sorted(rows) == sorted([(a_idp, a), (b_idp, b)])
    assert provider.token_calls == []  # no secret of A's or B's left the server


def test_the_installation_idp_secret_definer_refuses_a_client_context_and_other_idps(
    env: TestClient, world: World, app_db: Database, owner_session: Session
) -> None:
    """`shifde_installation_idp_secret` (B-2260), as `shifde_app` and as the schema owner: refused
    (42501) under A's or B's context; with no context it answers only for an active installation
    IdP, and B's IdP, a disabled installation IdP and an unknown id answer no row alike."""
    inst = create_idp(env, world)
    b_idp = create_idp(env, world, tenant="demo-b")
    off = create_idp(env, world, issuer="https://idp.test/off")
    owner_session.execute(
        text("UPDATE shifde.idp_configs SET status = 'disabled' WHERE id = :i"), {"i": off}
    )
    owner_session.commit()
    inst_secret, _ = _secret_of(owner_session, inst)
    a, b = tenant_id(owner_session, "demo-a"), tenant_id(owner_session, "demo-b")
    for tenant in (a, b):
        with app_db.session() as s:
            set_tenant(s, tenant)
            for idp in (inst, b_idp):
                with pytest.raises(DBAPIError) as err, s.begin_nested():
                    s.execute(text(DEFINER), {"i": idp})
                assert getattr(err.value.orig, "sqlstate", None) == "42501"
        owner_session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
        )
        with pytest.raises(DBAPIError) as err:
            owner_session.execute(text(DEFINER), {"i": inst})
        assert getattr(err.value.orig, "sqlstate", None) == "42501"
        owner_session.rollback()
    with app_db.session() as s:
        assert s.execute(text(DEFINER), {"i": inst}).scalars().all() == [inst_secret]
        for other in (b_idp, off, str(uuid7())):
            assert s.execute(text(DEFINER), {"i": other}).all() == []
        s.rollback()
    for other in (b_idp, off, str(uuid7())):
        assert owner_session.execute(text(DEFINER), {"i": other}).all() == []
    owner_session.rollback()


def test_a_client_admin_cannot_reach_the_installation_idp_secret(
    env: TestClient, world: World, owner_session: Session
) -> None:
    """The installation IdP's secret is not one of A's secrets for A's owner (list, rotate,
    change answer like an unknown id), nor is B's IdP's secret; the platform IdP routes are the
    one 403 for a client owner."""
    inst = create_idp(env, world)
    b_idp = create_idp(env, world, tenant="demo-b")
    inst_secret, _ = _secret_of(owner_session, inst)
    b_secret, _ = _secret_of(owner_session, b_idp)
    h = login(env, world, "admin@demo-a.local")
    listed = {x["id"] for x in env.get("/api/v1/orgs/demo-a/secrets", headers=h).json()["items"]}
    assert str(inst_secret) not in listed and str(b_secret) not in listed
    unknown = uuid7()
    for secret in (inst_secret, b_secret, unknown):
        rot = env.post(
            f"/api/v1/orgs/demo-a/secrets/{secret}/rotate",
            json={"value": "another-value-123", "row_version": 1},
            headers=h,
        )
        patch = env.patch(
            f"/api/v1/orgs/demo-a/secrets/{secret}",
            json={"status": "disabled", "row_version": 1},
            headers=h,
        )
        assert (rot.status_code, patch.status_code) == (404, 404), (rot.text, patch.text)
    assert env.get(f"/api/v1/platform/idps/{inst}", headers=h).status_code == 403
    assert env.get("/api/v1/platform/idps", headers=h).status_code == 403


def test_sync_never_adds_a_person_to_a_group_bound_only_in_another_client(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    """A person synced into a platform group bound only in B gains nothing in A: A's routes do
    not show the group or the person, A's audit holds no row of the sync, the person cannot
    read A; B holds the counts-only row."""
    a, b = tenant_id(owner_session, "demo-a"), tenant_id(owner_session, "demo-b")
    idp = create_idp(env, world, group_sync="sync")
    in_b = platform_group(env, world, "in-b", idp, "In B")
    in_a = platform_group(env, world, "in-a", idp, "In A")
    bind_group(env, world, "demo-b", in_b, ["workspace:read"], b)
    bind_group(env, world, "demo-a", in_a, ["workspace:read"], a)
    person = new_person(owner_session, "synced@example.com")
    link(owner_session, person, idp, "synced")
    r = ok_login(env, provider, idp, sub="synced", email="synced@example.com", groups=["In B"])
    assert r.status_code == 302 and r.headers["location"] == "/", r.headers
    assert env.get("/api/v1/orgs/demo-a").status_code == 404
    assert env.get("/api/v1/orgs/demo-b/me/permissions").json()["permissions"] == ["workspace:read"]
    env.cookies.clear()
    rows = owner_session.execute(
        text("SELECT group_id::text FROM shifde.group_members WHERE user_id = :u"),
        {"u": person},
    ).scalars()
    assert list(rows) == [in_b]
    owner_session.rollback()
    assert events(owner_session, "identity.group.members_changed", a) == []
    changed = events(owner_session, "identity.group.members_changed", b)
    assert [(d["group_id"], d["added_count"]) for d in changed] == [(in_b, 1)]
    assert str(person) not in str(changed) and "synced@example.com" not in str(changed)
    h = login(env, world, "admin@demo-a.local")
    groups = env.get("/api/v1/orgs/demo-a/groups", headers=h).json()["items"]
    assert [g["id"] for g in groups] == [in_a]
    members = env.get("/api/v1/orgs/demo-a/members", headers=h).json()["items"]
    assert str(person) not in str(members)
