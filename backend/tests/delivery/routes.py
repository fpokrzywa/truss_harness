"""Delivery route tests: `delivery.api.router` behind `require_auth` on the identity harness app
(login routes, error handlers); the `app` port mounts the router. Every route runs as
`shifde_app` under row-level security.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JSON bodies, TestClient options
from uuid import UUID

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery import api
from shifde.identity.dependencies import require_auth
from tests.identity import harness


@contextmanager
def delivery_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


def targets(slug: str) -> str:
    return f"/api/v1/orgs/{slug}/delivery/targets"


def address(suffix: str = "cloud.databricks.com") -> str:
    """A workspace URL no other test uses."""
    return f"https://adb-{uuid7().hex[-16:]}.{suffix}"


def target_body(workspace: str, url: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "workspace": workspace,
        "name": f"target-{uuid7().hex[-8:]}",
        "address": url,
        "environment": "dev",
        "bindings": {"warehouse_id": "abc123def456"},
        "credential_ref": "databricks-dev",
    }
    body.update(overrides)
    return body


def register(
    client: TestClient, h: dict[str, str], slug: str, body: dict[str, Any], status: int = 201
) -> Any:  # Any: a JSON body
    r = client.post(targets(slug), json=body, headers=h)
    assert r.status_code == status, r.text
    return r.json()


def counts(owner_db: Database, tenant: UUID) -> tuple[int, int]:
    """(targets, claims) of `tenant`, read as the schema owner."""
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_targets WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_platform_claims WHERE tenant_id = :t)"
            ),
            {"t": tenant},
        ).one()
    return int(row[0]), int(row[1])
