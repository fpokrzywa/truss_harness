"""Delivery route tests: `delivery.api.router` behind `require_auth` on the identity harness app
(login routes, error handlers), and `delivery.gateway_api.router` without it (executor tokens
only, with the job lease helpers); the `app` port mounts both routers. Every route runs as
`shifde_app` under row-level security.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any  # Any: JSON bodies, TestClient options, rows
from uuid import UUID

import httpx
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery import api, gateway, gateway_api
from shifde.identity.dependencies import require_auth
from tests.identity import harness
from tests.tenancy.conftest import app_session


@contextmanager
def delivery_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    app.include_router(gateway_api.router, dependencies=guards[:2])
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


# ------------------------------------------------------------------ executors
WHOAMI = "/api/executor/v1/whoami"


def executors(slug: str) -> str:
    return f"/api/v1/orgs/{slug}/delivery/executors"


def create_executor(
    client: TestClient, h: dict[str, str], slug: str, status: int = 201
) -> Any:  # Any: a JSON body
    r = client.post(executors(slug), json={"name": f"executor-{uuid7().hex[-8:]}"}, headers=h)
    assert r.status_code == status, r.text
    return r.json()


def issue(
    client: TestClient, h: dict[str, str], slug: str, executor_id: str, status: int = 201
) -> Any:  # Any: a JSON body ({credential, secret})
    r = client.post(f"{executors(slug)}/{executor_id}/credentials", headers=h)
    assert r.status_code == status, r.text
    return r.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ executor gateway jobs
LEASE = "/api/executor/v1/lease"


def job_url(job_id: object, action: str) -> str:
    return f"/api/executor/v1/jobs/{job_id}/{action}"


def enqueue(app_db: Database, tenant: UUID, kind: str, **values: Any) -> UUID:
    """A queued job of `tenant`, written as `shifde_app` through `gateway.enqueue_job`."""
    with app_session(app_db, tenant) as s:
        job_id = gateway.enqueue_job(s, tenant_id=tenant, kind=kind, **values)
        s.commit()
    return job_id


def lease(
    client: TestClient,
    token: str,
    kinds: Sequence[str] = ("plan", "apply", "observe", "verify", "reconcile"),
    request_id: UUID | None = None,
) -> httpx.Response:
    body = {"request_id": str(request_id or uuid7()), "kinds": list(kinds)}
    return client.post(LEASE, json=body, headers=bearer(token))


def proof(leased: Mapping[str, Any]) -> dict[str, Any]:  # Any: a JobLease body
    return {"lease_id": leased["lease_id"], "fence": leased["fence"]}


def owner_sql(owner_db: Database, sql: str, **params: Any) -> None:
    with owner_db.session() as o:
        o.execute(text(sql), params)
        o.commit()


def expire(owner_db: Database, job_id: object) -> None:
    """End the job's lease, as the schema owner."""
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_jobs SET lease_until = now() - interval '1 second' WHERE id = :j",
        j=job_id,
    )


def job_row(owner_db: Database, job_id: object) -> Any:  # Any: a RowMapping
    with owner_db.session() as o:
        return (
            o.execute(text("SELECT * FROM shifde.delivery_jobs WHERE id = :j"), {"j": job_id})
            .mappings()
            .one()
        )


def journal_rows(owner_db: Database, tenant: UUID) -> list[tuple[Any, ...]]:
    with owner_db.session() as o:
        return [
            tuple(r)
            for r in o.execute(
                text(
                    "SELECT * FROM shifde.delivery_journal WHERE tenant_id = :t "
                    "ORDER BY deployment_id, step_key"
                ),
                {"t": tenant},
            ).all()
        ]


# ------------------------------------------------------------------ plans
DIGEST_T = "c" * 64  # a toolchain digest
FINGERPRINT = "d" * 64


def plans(slug: str) -> str:
    return f"/api/v1/orgs/{slug}/delivery/plans"


def request_plan(
    client: TestClient,
    h: dict[str, str],
    slug: str,
    target_id: object,
    status: int = 201,
    **overrides: Any,
) -> Any:  # Any: a JSON body
    body: dict[str, Any] = {
        "target_id": str(target_id),
        "application_key": "sales-app",
        "display_name": "Sales app",
    }
    body.update(overrides)
    r = client.post(plans(slug), json=body, headers=h)
    assert r.status_code == status, r.text
    return r.json()


def plan_value(steps: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """A valid `PlanJobResult` value (one `create` step unless `steps` is given)."""
    return {
        "steps": list(
            steps
            if steps is not None
            else [{"key": "deploy_app", "action": "create", "resource_key": "app"}]
        ),
        "snapshot": {
            "resources": [],
            "fingerprint": FINGERPRINT,
            "captured_at": "2026-11-19T10:00:00+00:00",
        },
        "adapter_version": "0.1.0",
        "toolchain_digest": DIGEST_T,
    }


def lease_plan(client: TestClient, token: str) -> dict[str, Any]:  # Any: a JobLease body
    r = lease(client, token, kinds=["plan"])
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def plan_result(
    client: TestClient, token: str, leased: Mapping[str, Any], **result: Any
) -> httpx.Response:
    """Post a `plan` job result under `leased` (default: succeeded with `plan_value()`)."""
    body = {**proof(leased), **(result or {"status": "succeeded", "value": plan_value()})}
    return client.post(job_url(leased["job"]["id"], "result"), json=body, headers=bearer(token))


def plan_row(owner_db: Database, plan_id: object) -> Any:  # Any: a RowMapping
    with owner_db.session() as o:
        return (
            o.execute(text("SELECT * FROM shifde.delivery_plans WHERE id = :p"), {"p": plan_id})
            .mappings()
            .one()
        )
