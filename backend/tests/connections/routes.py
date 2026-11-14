"""Connections part 2: the connection and egress routes over a minimal FastAPI app (B-1020 pattern).

The app is the identity harness (login routes, error handlers) plus `connections.api.router`
behind `require_auth` and the process-wide integration runtime (`app.state.integration`); the `app`
port mounts the router (B-501). People and clients are the tenancy route-test world (fresh slugs
and ids per test); every route runs as `shifde_app` under row-level security.

SETH's API shortcuts (`p3util`) are kept with SETH's names. What SETH did through routes that are
not ported yet (`POST /secrets`, `/roles`, `/role-bindings`: admin) is written here directly:
secrets through `secrets.service.create_secret` in the client's context, custom roles and their
bindings as the schema owner with every trigger active (`tests/authz/conftest.py:seed_bindings`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JSON bodies, TestClient options
from uuid import UUID

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.adapters.testservers import RestTestServer
from shifde.connections import api
from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity.dependencies import require_auth
from shifde.secrets import service as secrets_service
from tests._support.tenants import bind_user
from tests.identity import harness
from tests.tenancy.conftest import app_session, owner_member

REGISTRY = AdapterRegistry.load()
SECRET_PREFIX = "conn-test-"  # every secret these tests write; removed after each test
REST_TOKEN = "rest-secret-token-0001"


@contextmanager
def connections_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    app.state.integration = IntegrationRuntime(settings=settings, registry=REGISTRY)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


def org(slug: str) -> str:
    return f"/api/v1/orgs/{slug}"


def as_user(client: TestClient, email: str) -> dict[str, str]:
    """Sign in as `email` (a new session replaces the previous one); the CSRF header."""
    client.cookies.clear()
    return harness.login(client, email)


def post(
    client: TestClient, url: str, h: dict[str, str], body: dict[str, Any], status: int = 201
) -> Any:  # Any: a JSON body
    r = client.post(url, json=body, headers=h)
    assert r.status_code == status, r.text
    return r.json()


# ------------------------------------------------------------------ SETH p3util shortcuts
def connection(
    client: TestClient, h: dict[str, str], body: dict[str, Any], base: str, status: int = 201
) -> Any:
    """SETH's `connection`: a read connection that binds a secret carries the read-only
    confirmation unless the body says otherwise (design 5.3, B-1507)."""
    defaults: dict[str, Any] = {
        "scope": {},
        "auth_method": "none",
        "access_mode": "read",
        "config": {},
    }
    merged = {**defaults, **body}
    if merged.get("secret_id") and merged["access_mode"] == "read":
        merged.setdefault("read_only_confirmed", True)
    return post(client, f"{base}/connections", h, merged, status)


def activate(client: TestClient, h: dict[str, str], cid: str, base: str) -> dict[str, Any]:
    tested = client.post(f"{base}/connections/{cid}/test", headers=h)
    assert tested.status_code == 200 and tested.json()["outcome"] == "pass", tested.text
    row = client.get(f"{base}/connections/{cid}", headers=h).json()
    done = client.post(
        f"{base}/connections/{cid}/status",
        json={"status": "active", "row_version": row["row_version"]},
        headers=h,
    )
    assert done.status_code == 200, done.text
    out: dict[str, Any] = done.json()
    return out


def rest_body(
    server: RestTestServer, secret_id: str | None, name: str = "rest-api"
) -> dict[str, Any]:
    return {
        "name": name,
        "connection_type": "rest",
        "adapter_key": "rest.http",
        "config": {"base_url": server.base_url, "health_path": "/health"},
        "auth_method": "bearer" if secret_id else "none",
        **({"secret_id": secret_id} if secret_id else {}),
    }


# ------------------------------------------------------------------ direct writes
def secret(
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    value: str,
    *,
    scope_type: str = "tenant",
    scope_id: UUID | None = None,
) -> str:
    """A client secret written by the secrets service in that client's context (SETH:
    `POST /secrets`, an admin route not ported yet)."""
    with app_session(app_db, tenant) as s:
        meta = secrets_service.create_secret(
            s,
            settings.keyring,
            tenant_id=tenant,
            name=f"{SECRET_PREFIX}{uuid7().hex}",
            value=value,
            scope_type=scope_type,
            scope_id=scope_id,
            created_by=None,
        )
        s.commit()
    return str(meta.id)


def grant(
    owner_db: Database,
    tenant: UUID,
    user: UUID,
    permissions: list[str],
    *,
    scope_type: str = "tenant",
    scope_id: UUID | None = None,
) -> UUID:
    """A custom role of `tenant` with exactly `permissions`, bound to `user` at the scope, as the
    schema owner with the binding triggers active (SETH: `/roles` + `/role-bindings`)."""
    role = uuid7()
    with owner_db.session() as o:
        o.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
        o.execute(
            text("INSERT INTO shifde.roles (id, tenant_id, key, name) VALUES (:i, :t, :k, :k)"),
            {"i": role, "t": tenant, "k": f"r-{role.hex}"},
        )
        for perm in permissions:
            o.execute(
                text(
                    "INSERT INTO shifde.role_permissions (role_id, permission_key, tenant_id) "
                    "VALUES (:r, :p, :t)"
                ),
                {"r": role, "p": perm, "t": tenant},
            )
        o.execute(
            text(
                "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                "role_id, scope_type, scope_id) VALUES (:i, :t, 'user', :u, :r, :st, :si)"
            ),
            {
                "i": uuid7(),
                "t": tenant,
                "u": user,
                "r": role,
                "st": scope_type,
                "si": scope_id or tenant,
            },
        )
        o.commit()
    return role


def limited_user(
    owner_db: Database, tenant: UUID, permissions: list[str], *, label: str = "limited"
) -> tuple[str, UUID]:
    """SETH `p5util.limited_user`: a member of `tenant` whose only grants are `permissions`
    (one custom role at organization scope); returns (e-mail, user id)."""
    email = f"{label}-{uuid7().hex[-12:]}@route.test"
    with owner_db.session() as o:
        uid = harness.person(o, email)
        owner_member(o, tenant, uid)
        o.commit()
    grant(owner_db, tenant, uid, permissions)
    return email, uid


def system_role_user(owner_db: Database, tenant: UUID, role_key: str) -> str:
    """SETH `new_user(..., [system_role(role_key)])`: a member bound to a system role template
    at organization scope; returns the e-mail."""
    email = f"{role_key}-{uuid7().hex[-12:]}@route.test"
    with owner_db.session() as o:
        uid = harness.person(o, email)
        owner_member(o, tenant, uid)
        bind_user(o, tenant, uid, role_key)
        o.commit()
    return email
