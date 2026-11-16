"""OpenID Connect over the full app (B-2263): the app factory with the fake IdP's transport and
resolver (`tests/_support/fake_oidc.py`), IdPs created through the admin routes as SETH's
`test_p2_oidc.create_idp` did, and the start / finish steps of a browser flow."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JWT claims and JSON bodies
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.app import create_app
from shifde.core.settings import Settings
from tests._support.fake_oidc import (
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    Authorization,
    FakeOidcProvider,
    resolver,
)
from tests._support.fullapp import ORIGIN, World, login
from tests._support.p2util import post

DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
FAIL = "/login?error=oidc_failed"
PLATFORM = "platform@shifde.local"


@contextmanager
def oidc_app(settings: Settings, provider: FakeOidcProvider) -> Iterator[TestClient]:
    """The full app (as `shifde_app`) whose outbound HTTP reaches the fake IdP."""
    s = settings.model_copy(update={"oidc_jwks_min_refresh_seconds": 0})
    app = create_app(s, http_transport=provider.transport(), resolver=resolver)
    try:
        with TestClient(app, base_url="https://testserver", headers={"Origin": ORIGIN}) as c:
            with app.state.db.session() as check:
                assert check.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
            yield c
    finally:
        app.state.db.dispose()


def create_idp(c: TestClient, world: World, *, tenant: str | None = None, **overrides: Any) -> str:
    """SETH `test_p2_oidc.create_idp`: through `/platform/idps` (installation) or
    `/orgs/{tenant}/idps` (that client's owner), with a client secret."""
    if tenant is None:
        h = login(c, world, PLATFORM)
        url = "/api/v1/platform/idps"
    else:
        h = login(c, world, f"admin@{tenant}.local")
        url = f"/api/v1/orgs/{tenant}/idps"
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


def platform_group(c: TestClient, world: World, slug: str, idp: str, external: str) -> str:
    """A platform group mapped to the installation IdP's group `external` (B-928, B-932)."""
    h = login(c, world, PLATFORM)
    out = post(
        c,
        "/api/v1/platform/groups",
        h,
        {"slug": slug, "name": external, "idp_config_id": idp, "external_id": external},
    )
    c.cookies.clear()
    return str(out["id"])


def bind_group(
    c: TestClient, world: World, org: str, group: str, permissions: list[str], tenant: UUID
) -> None:
    """`org`'s owner creates a role holding `permissions` and binds the group to it there."""
    h = login(c, world, f"admin@{org}.local")
    base = f"/api/v1/orgs/{org}"
    role = post(
        c, f"{base}/roles", h, {"key": f"r-{group[-6:]}", "name": "r", "permissions": permissions}
    )["id"]
    r = c.post(
        f"{base}/role-bindings",
        headers=h,
        json={
            "subject_type": "group",
            "subject_id": group,
            "role_id": role,
            "scope": {"type": "tenant", "id": str(tenant)},
        },
    )
    assert r.status_code == 201, r.text
    c.cookies.clear()


def tenant_id(owner: Session, slug: str) -> UUID:
    found: UUID = owner.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": slug}
    ).scalar_one()
    owner.rollback()
    return found


def user_id(owner: Session, email: str) -> UUID:
    found: UUID = owner.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
    ).scalar_one()
    owner.rollback()
    return found


def link(owner: Session, user: UUID, idp: str, sub: str, *, issuer: str = ISSUER) -> None:
    """The person's identity at the IdP, as a setup-token link would have made it."""
    owner.execute(
        text(
            "INSERT INTO shifde.user_identities (id, user_id, issuer, subject, idp_config_id) "
            "VALUES (gen_random_uuid(), :u, :i, :s, :c)"
        ),
        {"u": user, "i": issuer, "s": sub, "c": idp},
    )
    owner.commit()


def new_person(owner: Session, email: str, *, platform_admin: bool = False) -> UUID:
    uid: UUID = owner.execute(
        text(
            "INSERT INTO shifde.users (id, primary_email, display_name, is_platform_admin) "
            "VALUES (gen_random_uuid(), :e, :e, :p) RETURNING id"
        ),
        {"e": email, "p": platform_admin},
    ).scalar_one()
    owner.commit()
    return uid


def start(c: TestClient, provider: FakeOidcProvider, idp: str) -> Authorization:
    r = c.get(f"/api/auth/oidc/{idp}/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    assert "/authorize?" in r.headers["location"], r.headers
    return provider.parse_authorization(r.headers["location"])


def finish(c: TestClient, idp: str, code: str, state: str) -> httpx.Response:
    return c.get(
        f"/api/auth/oidc/{idp}/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def ok_login(
    c: TestClient, provider: FakeOidcProvider, idp: str, code: str = "c1", **claims: Any
) -> httpx.Response:
    """SETH `ok_login`: a full sign-in; the session cookie stays on the client."""
    auth = start(c, provider, idp)
    provider.issue_code(auth, provider.claims_for(auth, **claims), code=code)
    return finish(c, idp, code, auth.state)


def events(owner: Session, action: str, tenant: UUID | None) -> list[dict[str, Any]]:
    rows = owner.execute(
        text(
            "SELECT details FROM shifde.audit_events WHERE action = :a "
            "AND tenant_id IS NOT DISTINCT FROM :t ORDER BY id"
        ),
        {"a": action, "t": tenant},
    ).scalars()
    out = list(rows)
    owner.rollback()
    return out
