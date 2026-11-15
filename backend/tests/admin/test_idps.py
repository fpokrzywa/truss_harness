"""Identity-provider administration routes (admin part 3a, DECISIONS B-1621..B-1624).

Moved from SETH 3ba0e06 `tests/test_p2_oidc.py` (`test_idp_crud_is_write_only_for_the_secret_and_
audited`, `test_idp_urls_are_validated_and_the_test_endpoint_is_ssrf_guarded`, `test_installation_
idps_are_listed_in_auth_config_and_platform_only`) and `tests/test_p2_review.py` (`test_issuers_
are_unique_...`, `test_oidc_link_works_only_...`, `test_tenant_idps_cannot_enable_jit`,
`test_email_verified_is_ignored_...`), assertions unchanged except where B-1622 says. SETH's
seeded world is the tenancy route world: `demo-a`/`demo-b` are clients A and B, their
administrators A's and B's owners, `platform@shifde.local` the platform administrator
(`tests/tenancy/routes.py::make_people`); the app is `tests/admin/conftest.py::admin_client`
over the fake IdP (`tests/_support/fake_oidc.py`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any  # Any: JSON bodies
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeOidcProvider, resolver
from tests._support.tenants import bind_user
from tests.admin.conftest import admin_client
from tests.identity import harness
from tests.identity.test_oidc import FAIL, failure_reasons, finish, owner_invite, start
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"


def login(c: TestClient, email: str) -> dict[str, str]:
    """SETH `p2util.login`: the cookie jar is cleared first (a presented cookie is rotated)."""
    c.cookies.clear()
    return harness.login(c, email)


def post(c: TestClient, url: str, headers: dict[str, str], body: Any, status: int = 201) -> Any:
    r = c.post(url, json=body, headers=headers)
    assert r.status_code == status, f"{url}: {r.status_code} {r.text}"
    return r.json() if r.content else None


def _idp_body(issuer: str, **over: object) -> dict[str, object]:
    return {
        "display_name": "x",
        "issuer": issuer,
        "discovery_url": f"{issuer}/.well-known/openid-configuration",
        "client_id": CLIENT_ID,
        **over,
    }


@pytest.fixture
def provider() -> FakeOidcProvider:
    return FakeOidcProvider()


@pytest.fixture
def env(settings: Settings, world: World, provider: FakeOidcProvider) -> Iterator[TestClient]:
    """The admin app wired to the fake IdP (no network); JWKS refresh is unthrottled."""
    s = settings.model_copy(update={"oidc_jwks_min_refresh_seconds": 0})
    with admin_client(s, http_transport=provider.transport(), resolver=resolver) as c:
        yield c


def _slug(world: World, tenant: str) -> str:
    return {"demo-a": world.a.slug, "demo-b": world.b.slug}[tenant]


def create_idp(
    c: TestClient, world: World, people: People, *, tenant: str | None = None, **overrides: Any
) -> str:
    """SETH `test_p2_oidc.create_idp`: through the admin routes, with a client secret."""
    if tenant is None:
        h = login(c, people.platform)
        url = "/api/v1/platform/idps"
    else:
        h = login(c, people.owner_a if tenant == "demo-a" else people.owner_b)
        url = f"/api/v1/orgs/{_slug(world, tenant)}/idps"
    body = {
        "display_name": "Test IdP",
        "issuer": ISSUER,
        "discovery_url": DISCOVERY,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "jit": "create_user" if tenant is None else "off",  # tenant IdPs never provision
        **overrides,
    }
    out = post(c, url, h, body)
    c.cookies.clear()
    return str(out["id"])


def prelink_member(
    owner: Session, tenant: UUID | None, email: str, idp_id: Any, sub: str, *, issuer: str = ISSUER
) -> UUID:
    """SETH `p2util.prelink_member`: a person (member of `tenant` if given) already bound to the
    IdP, as a setup-token link would have done; returns the user id. A member is also a viewer
    there: since B-1009 a membership without a live role binding gives no access (B-1622)."""
    uid: UUID = owner.execute(
        text(
            "INSERT INTO shifde.users (id, primary_email, display_name) "
            "VALUES (gen_random_uuid(), :e, :e) RETURNING id"
        ),
        {"e": email},
    ).scalar_one()
    if tenant is not None:
        owner.execute(
            text(
                "INSERT INTO shifde.memberships (id, tenant_id, user_id) "
                "VALUES (gen_random_uuid(), :t, :u)"
            ),
            {"t": tenant, "u": uid},
        )
        bind_user(owner, tenant, uid, "viewer")
    owner.execute(
        text(
            "INSERT INTO shifde.user_identities (id, user_id, issuer, subject, idp_config_id) "
            "VALUES (gen_random_uuid(), :u, :i, :s, :c)"
        ),
        {"u": uid, "i": issuer, "s": sub, "c": idp_id},
    )
    owner.commit()
    return uid


# ---------------------------------------------------------------- SETH test_p2_oidc.py
def test_idp_crud_is_write_only_for_the_secret_and_audited(
    env: TestClient, world: World, people: People, owner_session: Session
) -> None:
    h = login(env, people.owner_a)
    base = f"/api/v1/orgs/{world.a.slug}/idps"
    created = post(
        env,
        base,
        h,
        {
            "display_name": "Corp",
            "issuer": ISSUER,
            "discovery_url": DISCOVERY,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    assert created["client_secret_set"] is True and CLIENT_SECRET not in str(created)
    assert (
        created["tenant_id"] is not None
        and created["jit"] == "off"
        and created["group_sync"] == "off"
    )
    got = env.get(f"{base}/{created['id']}", headers=h)
    assert CLIENT_SECRET not in got.text and "client_secret_ref" not in got.text
    # B-1622: a client's IdP never syncs platform groups (B-932); SETH turned sync on here
    refused = env.patch(
        f"{base}/{created['id']}",
        json={"display_name": "Corp 2", "group_sync": "sync", "row_version": 1},
        headers=h,
    )
    assert refused.status_code == 422, refused.text
    upd = env.patch(
        f"{base}/{created['id']}",
        json={"display_name": "Corp 2", "group_sync": "off", "row_version": 1},
        headers=h,
    )
    assert (
        upd.status_code == 200
        and upd.json()["group_sync"] == "off"
        and upd.json()["row_version"] == 2
    )
    assert (
        env.patch(
            f"{base}/{created['id']}", json={"display_name": "x", "row_version": 1}, headers=h
        ).status_code
        == 409
    )
    rot = env.patch(
        f"{base}/{created['id']}",
        json={"client_secret": "another-secret-456", "row_version": 2},
        headers=h,
    )
    assert rot.status_code == 200 and "another-secret-456" not in rot.text
    secret_rows = owner_session.execute(
        text("SELECT kind, secret_version, tenant_id FROM shifde.secrets")
    ).all()
    # the secret is the IdP's own client's (B-924)
    assert [tuple(r) for r in secret_rows] == [("idp_client_secret", 2, world.a.tenant)]
    # the secret exists only encrypted in the database
    assert CLIENT_SECRET not in str(
        owner_session.execute(text("SELECT to_jsonb(s) FROM shifde.secrets s")).scalar_one()
    )
    actions = [
        r[0]
        for r in owner_session.execute(
            text(
                "SELECT action FROM shifde.audit_events WHERE action LIKE 'identity.idp.%' "
                "ORDER BY id"
            )
        )
    ]
    assert actions == ["identity.idp.created", "identity.idp.updated", "identity.idp.updated"]
    assert env.get(f"/api/v1/orgs/{world.b.slug}/idps", headers=h).status_code == 404


def test_idp_urls_are_validated_and_the_test_endpoint_is_ssrf_guarded(
    settings: Settings, world: World, people: People, provider: FakeOidcProvider
) -> None:
    def private(host: str) -> list[str]:
        return ["10.0.0.5"] if host == "internal.test" else ["93.184.216.34"]

    with admin_client(settings, http_transport=provider.transport(), resolver=private) as c:
        h = login(c, people.owner_a)
        base = f"/api/v1/orgs/{world.a.slug}/idps"
        body = {"display_name": "x", "issuer": ISSUER, "discovery_url": DISCOVERY, "client_id": "c"}
        for bad in (
            "http://idp.test/.well-known/openid-configuration",
            "http://127.0.0.1:9/x",
            "ftp://idp.test/x",
            "https://u:p@idp.test/x",
        ):
            r = c.post(base, json={**body, "discovery_url": bad}, headers=h)
            assert r.status_code == 422, bad  # https only (loopback http is development-only)
        ok = post(c, base, h, body)
        good = c.post(f"{base}/{ok['id']}/test", headers=h)
        assert good.status_code == 200 and good.json()["ok"] is True
        assert [x["name"] for x in good.json()["checks"]] == ["discovery", "jwks"]
        assert all({"name", "ok", "detail"} == set(x) for x in good.json()["checks"])
        internal = post(
            c,
            base,
            h,
            {
                **body,
                "display_name": "int",
                "issuer": "https://internal.test",
                "discovery_url": "https://internal.test/.well-known/openid-configuration",
            },
        )
        bad = c.post(f"{base}/{internal['id']}/test", headers=h).json()
        assert bad["ok"] is False and "non-public" in bad["checks"][0]["detail"]


def test_installation_idps_are_listed_in_auth_config_and_platform_only(
    env: TestClient, world: World, people: People, provider: FakeOidcProvider
) -> None:
    idp = create_idp(env, world, people)
    cfg = env.get("/api/auth/config").json()
    assert cfg["oidc_providers"] == [
        {"id": idp, "display_name": "Test IdP", "login_url": f"/api/auth/oidc/{idp}/login"}
    ]
    assert CLIENT_SECRET not in str(cfg)
    h = login(env, people.owner_a)
    assert env.get("/api/v1/platform/idps", headers=h).status_code == 403
    tenant_idp = create_idp(
        env, world, people, tenant="demo-b", display_name="B only", issuer="https://b-only.test"
    )
    assert [p["id"] for p in env.get("/api/auth/config").json()["oidc_providers"]] == [
        idp
    ]  # no cross-tenant listing
    h = login(env, people.owner_a)
    assert env.get(f"/api/v1/orgs/{world.a.slug}/idps/{tenant_idp}", headers=h).status_code == 404
    # a disabled IdP cannot be used to log in
    h = login(env, people.platform)
    env.patch(
        f"/api/v1/platform/idps/{idp}", json={"status": "disabled", "row_version": 1}, headers=h
    )
    env.cookies.clear()
    assert (
        env.get(f"/api/auth/oidc/{idp}/login", follow_redirects=False).headers["location"] == FAIL
    )
    assert env.get("/api/auth/config").json()["oidc_providers"] == []


# ---------------------------------------------------------------- SETH test_p2_review.py
def test_issuers_are_unique_per_owner_scope_and_each_scope_signs_in_only_its_own_identities(
    env: TestClient,
    world: World,
    people: People,
    provider: FakeOidcProvider,
    owner_session: Session,
) -> None:
    a_url, b_url = f"/api/v1/orgs/{world.a.slug}", f"/api/v1/orgs/{world.b.slug}"
    # The installation and an org register the SAME issuer: both are allowed and both work.
    inst = create_idp(env, world, people)
    hb = login(env, people.owner_b)
    tenant_idp = post(
        env, f"{b_url}/idps", hb, _idp_body(ISSUER, client_secret="test-client-secret-value-123")
    )["id"]
    env.cookies.clear()
    member = prelink_member(
        owner_session, world.b.tenant, "member@b.example", tenant_idp, "s-tenant"
    )
    # installation login (JIT) for the installation user
    auth = start(env, provider, inst)
    code = provider.issue_code(
        auth, provider.claims_for(auth, sub="s-inst", email="inst@corp.example")
    )
    assert finish(env, inst, code, auth.state).headers["location"] == "/"
    inst_user = env.get("/api/auth/session").json()["user"]["id"]
    env.cookies.clear()
    # tenant login for the org member
    auth = start(env, provider, tenant_idp)
    code = provider.issue_code(
        auth, provider.claims_for(auth, sub="s-tenant", email="member@b.example")
    )
    assert finish(env, tenant_idp, code, auth.state).headers["location"] == "/"
    assert env.get("/api/auth/session").json()["user"]["id"] == str(member)
    env.cookies.clear()
    # neither can sign in the other's identity, even with the identical issuer string
    auth = start(env, provider, tenant_idp)
    code = provider.issue_code(
        auth, provider.claims_for(auth, sub="s-inst", email="inst@corp.example")
    )
    assert finish(env, tenant_idp, code, auth.state).headers["location"] == FAIL
    assert env.get("/api/auth/session").status_code == 401
    env.cookies.clear()
    auth = start(env, provider, inst)
    code = provider.issue_code(
        auth, provider.claims_for(auth, sub="s-tenant", email="member@b.example")
    )
    assert (
        finish(env, inst, code, auth.state).headers["location"] == FAIL
    )  # not the member's account
    assert env.get("/api/auth/session").status_code == 401
    bound = owner_session.execute(
        text(
            "SELECT idp_config_id::text, user_id::text FROM shifde.user_identities "
            "WHERE subject = 's-tenant'"
        )
    ).all()
    assert [tuple(r) for r in bound] == [(tenant_idp, str(member))]
    assert inst_user != str(member)
    # a tenant squatting an installation issuer neither blocks the installation nor reveals it:
    # conflicts exist only inside the caller's own scope
    again = env.post(f"{b_url}/idps", json=_idp_body(ISSUER), headers=login(env, people.owner_b))
    assert (
        again.status_code == 409 and again.json()["code"] == "issuer_conflict"
    )  # its own duplicate
    other = post(
        env, f"{b_url}/idps", login(env, people.owner_b), _idp_body("https://other-issuer.test")
    )
    patched = env.patch(
        f"{b_url}/idps/{other['id']}",
        json={"issuer": ISSUER, "row_version": 1},
        headers=login(env, people.owner_b),
    )
    assert patched.status_code == 409 and patched.json()["code"] == "issuer_conflict"
    ha = login(env, people.owner_a)
    assert (
        env.post(f"{a_url}/idps", json=_idp_body(ISSUER), headers=ha).status_code == 201
    )  # other org: fine
    hp = login(env, people.platform)
    dup_inst = env.post("/api/v1/platform/idps", json=_idp_body(ISSUER), headers=hp)
    assert dup_inst.status_code == 409 and dup_inst.json()["code"] == "issuer_conflict"
    with pytest.raises(DBAPIError, match="uq_idp_configs__scope_issuer"):
        owner_session.execute(
            text(
                "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, "
                "discovery_url, client_id) VALUES (gen_random_uuid(), :t, 'x', :i, "
                "'https://e/x', 'c')"
            ),
            {"t": world.b.tenant, "i": ISSUER},
        )
    owner_session.rollback()
    # the identity lookup is bound to the IdP: another IdP never resolves a foreign subject
    row = owner_session.execute(
        text("SELECT * FROM shifde.shifde_resolve_identity(:i, 's-inst', NULL, :c)"),
        {"i": ISSUER, "c": tenant_idp},
    ).first()
    assert row is None


def test_oidc_link_works_only_through_installation_or_same_tenant_idps(
    env: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a_idp = create_idp(env, world, people, tenant="demo-a", issuer="https://a.test")
    b_idp = create_idp(env, world, people, tenant="demo-b", issuer="https://b.test")
    inst = create_idp(env, world, people, issuer="https://inst.test")
    tid_a = world.a.tenant
    uid = owner_session.execute(
        text(
            "INSERT INTO shifde.users (id, primary_email, display_name) "
            "VALUES (gen_random_uuid(), 'l@example.com', 'l') RETURNING id"
        )
    ).scalar_one()
    owner_session.execute(
        text(
            "INSERT INTO shifde.memberships (id, tenant_id, user_id) "
            "VALUES (gen_random_uuid(), :t, :u)"
        ),
        {"t": tid_a, "u": uid},
    )
    token = owner_invite(owner_session, uid, tid_a)
    owner_session.execute(
        text("SELECT * FROM shifde.shifde_redeem_setup_token(:h, NULL, 3600)"),
        {"h": hashlib.sha256(token.encode()).digest()},
    )
    owner_session.commit()

    def resolve(idp: str, issuer: str, sub: str) -> object:
        r = owner_session.execute(
            text("SELECT user_id FROM shifde.shifde_resolve_identity(:i, :s, 'l@example.com', :c)"),
            {"i": issuer, "s": sub, "c": idp},
        ).first()
        owner_session.commit()
        return r

    assert resolve(b_idp, "https://b.test", "s-b") is None  # another tenant IdP: refused
    assert resolve(a_idp, "https://a.test", "s-a") is not None  # the issuing tenant IdP: linked
    # a bootstrap-style (tenant-less) token links through installation IdPs only
    root = owner_session.execute(
        text(
            "INSERT INTO shifde.users (id, primary_email, display_name) "
            "VALUES (gen_random_uuid(), 'r@example.com', 'r') RETURNING id"
        )
    ).scalar_one()
    t2 = owner_invite(owner_session, root, None)
    owner_session.execute(
        text("SELECT * FROM shifde.shifde_redeem_setup_token(:h, NULL, 3600)"),
        {"h": hashlib.sha256(t2.encode()).digest()},
    )
    owner_session.commit()

    def r2(idp: str, iss: str, sub: str) -> object:
        return owner_session.execute(
            text("SELECT user_id FROM shifde.shifde_resolve_identity(:i, :s, 'r@example.com', :c)"),
            {"i": iss, "s": sub, "c": idp},
        ).first()

    assert r2(a_idp, "https://a.test", "s-root-a") is None
    assert r2(inst, "https://inst.test", "s-root-i") is not None
    owner_session.commit()


def test_tenant_idps_cannot_enable_jit(
    env: TestClient, world: World, people: People, owner_session: Session
) -> None:
    a_url = f"/api/v1/orgs/{world.a.slug}"
    h = login(env, people.owner_a)
    for jit in ("create_user", "create_and_join"):
        r = env.post(f"{a_url}/idps", json=_idp_body("https://jit.test", jit=jit), headers=h)
        assert r.status_code == 422, jit
    ok = post(env, f"{a_url}/idps", h, _idp_body("https://jit.test"))
    assert ok["jit"] == "off"
    r = env.patch(
        f"{a_url}/idps/{ok['id']}", json={"jit": "create_user", "row_version": 1}, headers=h
    )
    assert r.status_code == 422
    with pytest.raises(DBAPIError, match="ck_idp_configs__tenant_no_jit"):
        owner_session.execute(
            text("UPDATE shifde.idp_configs SET jit = 'create_user' WHERE id = :i"), {"i": ok["id"]}
        )
    owner_session.rollback()
    hp = login(env, people.platform)  # installation IdPs may
    inst = env.post(
        "/api/v1/platform/idps",
        json=_idp_body("https://inst-jit.test", jit="create_and_join"),
        headers=hp,
    )
    assert inst.status_code == 201 and inst.json()["jit"] == "create_and_join"


def test_email_verified_is_ignored_for_custom_email_claim_mappings(
    env: TestClient,
    world: World,
    people: People,
    provider: FakeOidcProvider,
    owner_session: Session,
) -> None:
    idp = create_idp(env, world, people, claim_mappings={"email": "mail", "groups": "groups"})
    auth = start(env, provider, idp)
    code = provider.issue_code(
        auth, provider.claims_for(auth, email=None, mail="spoof@example.com", email_verified=True)
    )
    assert finish(env, idp, code, auth.state).headers["location"] == FAIL
    assert failure_reasons(owner_session)[-1] == "jit_requires_verified_email"
