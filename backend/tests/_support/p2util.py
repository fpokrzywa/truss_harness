"""API shortcuts of the full-app tests (SETH 3ba0e06 `tests/p2util.py`, the parts they use).

`new_user` differs from SETH on purpose (B-928, B-1401..B-1406): people are created only by the
platform administrator, so the helper logs in as `platform@shifde.local` (password remembered from
the last `login` call), creates and redeems, and leaves the new person's session open.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

PLATFORM = "platform@shifde.local"
_last_passwords: dict[str, str] = {}


def tenant_id(owner: Session, slug: str) -> Any:
    return owner.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": slug}
    ).scalar_one()


def system_role(owner: Session, key: str) -> Any:
    return owner.execute(
        text("SELECT id FROM shifde.roles WHERE tenant_id IS NULL AND key = :k"), {"k": key}
    ).scalar_one()


def login(client: TestClient, passwords: dict[str, str], email: str) -> dict[str, str]:
    client.cookies.clear()
    _last_passwords.update(passwords)
    r = client.post("/api/auth/local/login", json={"email": email, "password": passwords[email]})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def post(
    client: TestClient, url: str, headers: dict[str, str], body: Any, status: int = 201
) -> Any:
    r = client.post(url, json=body, headers=headers)
    assert r.status_code == status, f"{url}: {r.status_code} {r.text}"
    return r.json() if r.content else None


def new_user(
    client: TestClient,
    headers: dict[str, str],
    org: str,
    email: str,
    password: str,
    role_ids: list[str] | None = None,
) -> str:
    """The platform administrator adds a user to `org`; the user redeems the setup token (sets a
    password). `headers` (the caller's) is not used: the unified platform lets only the platform
    administrator create people. The caller must log back in afterwards (redemption opens the new
    user's session). Setup differs from SETH, not the behaviour under test."""
    platform_h = login(client, _last_passwords, PLATFORM)
    created = post(
        client,
        f"/api/v1/orgs/{org}/users",
        platform_h,
        {"email": email, "display_name": email.split("@")[0], "role_ids": role_ids or []},
    )
    redeemed = client.post(
        "/api/auth/setup/redeem", json={"token": created["setup_token"], "password": password}
    )
    assert redeemed.status_code == 200, redeemed.text
    return str(created["user"]["user"]["id"])


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def make_key(
    client: TestClient,
    h: dict[str, str],
    owner: Session,
    *,
    role: str = "tenant_administrator",
    scopes: list[str] | None = None,
    name: str = "ci",
    resource_scope: dict[str, list[str]] | None = None,
    days: int = 30,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A service account of `demo-a` holding `role`, with an API key (SETH `test_p2_credentials`)."""
    org = "/api/v1/orgs/demo-a"
    sa = post(client, f"{org}/service-accounts", h, {"name": name})
    bound = client.post(
        f"{org}/role-bindings",
        headers=h,
        json={
            "subject_type": "service_account",
            "subject_id": sa["id"],
            "role_id": str(system_role(owner, role)),
            "scope": {"type": "tenant", "id": str(tenant_id(owner, "demo-a"))},
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
    return sa, post(client, f"{org}/api-keys", h, body)
