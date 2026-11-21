"""OIDC helpers for the identity cross-client and hardening tests: one fake IdP host per client
(`tests/_support/fake_oidc.py` serves each host's own issuer), IdP rows inserted as the owner,
and the start / finish steps of a browser flow over `tests/identity/harness.py` (`shifde_app`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JWT claims
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.fake_oidc import (
    CLIENT_ID,
    Authorization,
    FakeOidcProvider,
    entra_issuer,
    resolver,
)
from tests.identity import harness

FAIL = "/login?error=oidc_failed"


def sso(name: str) -> str:
    """The issuer of client `name`'s IdP (distinct from the `world` IdPs' issuers)."""
    return f"https://sso.{name}.test"


def owner_oidc_idp(
    owner: Session,
    tenant: UUID | None,
    issuer: str,
    *,
    status: str = "active",
    group_sync_mode: str = "off",
    jit: str = "off",
) -> UUID:
    """An IdP at the fake provider without a client secret (`require_secret=False`)."""
    iid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, discovery_url, "
            "client_id, status, group_sync_mode, jit) VALUES (:id, :t, :i, :i, :d, :c, :s, :g, :j)"
        ),
        {
            "id": iid,
            "t": tenant,
            "i": issuer,
            "d": f"{issuer}/.well-known/openid-configuration",
            "c": CLIENT_ID,
            "s": status,
            "g": group_sync_mode,
            "j": jit,
        },
    )
    return iid


def owner_entra_idp(
    owner: Session, entra_tenant: str, *, group_sync_mode: str = "off", jit: str = "create_user"
) -> UUID:
    """An installation `entra` IdP pinned to `entra_tenant`, at the fake's Entra mode, without a
    client secret (`require_secret=False`)."""
    iid = uuid7()
    issuer = entra_issuer(entra_tenant)
    owner.execute(
        text(
            "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, discovery_url, "
            "client_id, group_sync_mode, jit, profile, entra_tenant_id) "
            "VALUES (:id, NULL, 'Entra', :i, :d, :c, :g, :j, 'entra', :e)"
        ),
        {
            "id": iid,
            "i": issuer,
            "d": f"{issuer}/.well-known/openid-configuration",
            "c": CLIENT_ID,
            "g": group_sync_mode,
            "j": jit,
            "e": entra_tenant,
        },
    )
    return iid


def owner_identity(owner: Session, user: UUID, idp: UUID, issuer: str, subject: str) -> None:
    owner.execute(
        text(
            "INSERT INTO shifde.user_identities (id, user_id, issuer, subject, idp_config_id) "
            "VALUES (:id, :u, :i, :s, :c)"
        ),
        {"id": uuid7(), "u": user, "i": issuer, "s": subject, "c": idp},
    )


def provider() -> FakeOidcProvider:
    return FakeOidcProvider(require_secret=False)


@contextmanager
def oidc_client(settings: Settings, fake: FakeOidcProvider) -> Iterator[TestClient]:
    s = settings.model_copy(update={"oidc_jwks_min_refresh_seconds": 0})
    transport: httpx.BaseTransport = fake.transport()
    with harness.client_for(s, http_transport=transport, resolver=resolver) as c:
        db: Database = c.app.state.db
        with db.session() as check:
            assert check.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
        yield c


def start(c: TestClient, fake: FakeOidcProvider, idp: UUID) -> Authorization:
    r = c.get(f"/api/auth/oidc/{idp}/login", follow_redirects=False)
    assert r.status_code == 302 and "/authorize?" in r.headers["location"], r.headers
    return fake.parse_authorization(r.headers["location"])


def finish(c: TestClient, idp: UUID, code: str, state: str) -> httpx.Response:
    return c.get(
        f"/api/auth/oidc/{idp}/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def sign_in(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, issuer: str, code: str = "c1", **claims: Any
) -> httpx.Response:
    auth = start(c, fake, idp)
    fake.issue_code(auth, fake.claims_for(auth, iss=issuer, **claims), code=code)
    return finish(c, idp, code, auth.state)


def failures(owner: Session) -> list[tuple[str, UUID | None]]:
    """(reason, tenant_id) of every failed sign-in, oldest first."""
    rows = owner.execute(
        text(
            "SELECT details->>'reason', tenant_id FROM shifde.audit_events "
            "WHERE action = 'identity.login.failed' ORDER BY id"
        )
    ).all()
    owner.rollback()
    return [(r[0], r[1]) for r in rows]
