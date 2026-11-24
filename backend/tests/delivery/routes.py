"""Delivery route tests: `delivery.api.router` behind `require_auth` on the identity harness app
(login routes, error handlers), and `delivery.gateway_api.router` without it (executor tokens
only, with the job lease helpers); the `app` port mounts both routers. Every route runs as
`shifde_app` under row-level security.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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
from tests.connections.routes import as_user
from tests.delivery.releases import release_for_target
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


def snowflake_address() -> str:
    """A Snowflake account URL no other test uses (normalized form)."""
    return f"https://acct-{uuid7().hex[-16:]}.snowflakecomputing.com"


SNOWFLAKE_BINDINGS = {
    "package": "SHIFDE_PKG",
    "application": "SHIFDE_APP",
    "warehouse": "APP_WH",
    "cortex_model": "mistral-large2",
}


def snowflake_body(workspace: str, url: str, **overrides: Any) -> dict[str, Any]:
    """A Snowflake target body (default bindings `SNOWFLAKE_BINDINGS`)."""
    body = target_body(
        workspace,
        url,
        platform_kind="snowflake",
        bindings=dict(SNOWFLAKE_BINDINGS),
        credential_ref="snowflake-dev",
    )
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


def make_deployment(owner_db: Database, tenant: UUID) -> UUID:
    """A real `queued` deployment of `tenant` (B-2673 keys), inserted as the schema owner with an
    archived target of the organization's first workspace, an artifact and a `requested` plan, so
    a job or a journal step can name it. No apply job is queued for it."""
    target, artifact, plan, deployment_id = uuid7(), uuid7(), uuid7(), uuid7()
    with owner_db.session() as o:
        o.execute(
            text(
                "INSERT INTO shifde.delivery_targets (tenant_id, id, workspace_id, name, "
                "platform_kind, address, environment, bindings, credential_ref, status, "
                "created_by) SELECT :t, :g, w.id, 'helper target', 'databricks', :a, 'dev', "
                "'{}'::jsonb, 'databricks-dev', 'archived', :u FROM shifde.workspaces w "
                "WHERE w.tenant_id = :t ORDER BY w.id LIMIT 1"
            ),
            {"t": tenant, "g": target, "a": address(), "u": uuid7()},
        )
        o.execute(
            text(
                "INSERT INTO shifde.delivery_artifacts (tenant_id, id, target_kind, digest, size, "
                "content, created_by) VALUES (:t, :i, 'databricks.app/v1', :d, 1, :c, :u)"
            ),
            {"t": tenant, "i": artifact, "d": "e" * 64, "c": b"\x00", "u": uuid7()},
        )
        o.execute(
            text(
                "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, target_revision, "
                "artifact_id, artifact_digest, application_key, display_name, requested_by) "
                "VALUES (:t, :i, :g, 1, :a, :d, 'helper-app', 'Helper app', :u)"
            ),
            {"t": tenant, "i": plan, "g": target, "a": artifact, "d": "e" * 64, "u": uuid7()},
        )
        o.execute(
            text(
                "INSERT INTO shifde.delivery_deployments (tenant_id, id, plan_id, target_id, "
                "application_key) VALUES (:t, :i, :p, :g, 'helper-app')"
            ),
            {"t": tenant, "i": deployment_id, "p": plan, "g": target},
        )
        o.commit()
    return deployment_id


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
    release_id: object,
    status: int = 201,
    **overrides: Any,
) -> Any:  # Any: a JSON body
    body: dict[str, Any] = {
        "target_id": str(target_id),
        "release_id": str(release_id),
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


# ------------------------------------------------------------------ deployments
APP_RESOURCE = "databricks_app"
STEP = "bundle_deploy"
STEP_DIGEST = "f" * 64
BOTH_CHECKS = [
    {"name": "native-runtime", "status": "passed"},
    {"name": "runtime-warehouse", "status": "passed"},
]


def deployments(slug: str) -> str:
    return f"/api/v1/orgs/{slug}/delivery/deployments"


def binding(target_id: object, native_id: str = "sales-app") -> dict[str, Any]:
    """A `ResourceBinding` of a Databricks app on the target."""
    return {
        "key": "app",
        "native_id": native_id,
        "resource_type": APP_RESOURCE,
        "application_id": str(uuid7()),
        "target_id": str(target_id),
    }


def receipt_value(
    tenant: object,
    target_id: object,
    artifact_digest: str,
    resources: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """A valid `ApplyReceipt` value (one app on the target unless `resources` is given)."""
    return {
        "tenant_id": str(tenant),
        "application_id": str(uuid7()),
        "target_id": str(target_id),
        "artifact_digest": artifact_digest,
        "resources": list(resources if resources is not None else [binding(target_id)]),
        "operation_handles": {"deploy": "op-1"},
    }


def job_result(
    client: TestClient, token: str, leased: Mapping[str, Any], **result: Any
) -> httpx.Response:
    """Post a job result under `leased` (`status` and `value` or `error_code`)."""
    body = {**proof(leased), **result}
    return client.post(job_url(leased["job"]["id"], "result"), json=body, headers=bearer(token))


def checkpoint(
    client: TestClient,
    token: str,
    leased: Mapping[str, Any],
    sequence: int,
    state: str,
    receipt: Mapping[str, Any] | None = None,
) -> httpx.Response:
    body = {
        **proof(leased),
        "sequence": sequence,
        "step_key": STEP,
        "state": state,
        "input_digest": STEP_DIGEST,
        **({"receipt": dict(receipt)} if receipt is not None else {}),
    }
    return client.post(job_url(leased["job"]["id"], "checkpoint"), json=body, headers=bearer(token))


def deployment_row(owner_db: Database, deployment_id: object) -> Any:  # Any: a RowMapping
    with owner_db.session() as o:
        return (
            o.execute(
                text("SELECT * FROM shifde.delivery_deployments WHERE id = :d"),
                {"d": deployment_id},
            )
            .mappings()
            .one()
        )


def deployment_jobs(
    owner_db: Database, deployment_id: object, kind: str | None = None
) -> list[Any]:  # Any: RowMappings
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT *, now() AS db_now FROM shifde.delivery_jobs WHERE deployment_id = :d "
                    "AND (CAST(:k AS text) IS NULL OR kind = :k) ORDER BY created_at, id"
                ),
                {"d": deployment_id, "k": kind},
            )
            .mappings()
            .all()
        )


def make_due(owner_db: Database, deployment_id: object) -> None:
    """The deployment's queued jobs are due now (skips the 15 s poll delay), as the owner."""
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_jobs SET run_after = now() WHERE deployment_id = :d "
        "AND status = 'queued'",
        d=deployment_id,
    )


def lease_kind(client: TestClient, token: str, kind: str) -> dict[str, Any]:  # Any: a JobLease
    r = lease(client, token, kinds=[kind])
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


@dataclass
class Flow:
    """Organization A's delivery path through the routes: the requester registers a target and
    requests plans (each of a new sealed, passed release in the target's scope unless one is
    given), the executor seals them, a second tenant administrator approves them."""

    client: TestClient
    slug: str
    workspace: str
    tenant: UUID
    requester: str
    approver: str
    token: str
    owner_db: Database

    def target(self) -> dict[str, Any]:  # Any: a TargetOut body
        h = as_user(self.client, self.requester)
        made: dict[str, Any] = register(
            self.client, h, self.slug, target_body(self.workspace, address())
        )
        self.client.cookies.clear()
        return made

    def sealed(
        self,
        target_id: object,
        application_key: str = "sales-app",
        value: Mapping[str, Any] | None = None,
        release_id: object | None = None,
    ) -> dict[str, Any]:  # Any: a PlanOut body
        release = release_id or self.release(target_id)
        h = as_user(self.client, self.requester)
        made = request_plan(
            self.client, h, self.slug, target_id, release, application_key=application_key
        )
        self.client.cookies.clear()
        leased = lease_plan(self.client, self.token)
        assert leased["job"]["plan_id"] == made["id"]
        r = plan_result(
            self.client, self.token, leased, status="succeeded", value=value or plan_value()
        )
        assert r.status_code == 200, r.text
        return self.get_plan(made["id"])

    def release(self, target_id: object, **options: Any) -> UUID:
        """A new sealed release in the target's scope with a passed evaluation run."""
        return release_for_target(self.owner_db, target_id, **options).id

    def get_plan(self, plan_id: object) -> dict[str, Any]:  # Any: a PlanOut body
        h = as_user(self.client, self.requester)
        r = self.client.get(f"{plans(self.slug)}/{plan_id}", headers=h)
        self.client.cookies.clear()
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body

    def approve(self, plan: Mapping[str, Any]) -> dict[str, Any]:  # Any: a PlanOut body
        h = as_user(self.client, self.approver)
        r = self.client.post(
            f"{plans(self.slug)}/{plan['id']}/approve",
            json={"plan_digest": plan["digest"]},
            headers=h,
        )
        self.client.cookies.clear()
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body

    def deployment(
        self, target_id: object | None = None, application_key: str = "sales-app"
    ) -> tuple[dict[str, Any], str]:  # Any: the approved PlanOut body
        """An approved plan and its queued deployment's id (a new target unless given)."""
        target = target_id if target_id is not None else self.target()["id"]
        approved = self.approve(self.sealed(target, application_key))
        assert approved["deployment_id"] is not None
        return approved, approved["deployment_id"]

    def read(self, deployment_id: object, email: str | None = None) -> httpx.Response:
        h = as_user(self.client, email or self.requester)
        r = self.client.get(f"{deployments(self.slug)}/{deployment_id}", headers=h)
        self.client.cookies.clear()
        return r

    def get(self, deployment_id: object) -> dict[str, Any]:  # Any: a DeploymentDetail body
        r = self.read(deployment_id)
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body

    def act(
        self,
        deployment_id: object,
        action: str,
        body: Mapping[str, Any] | None = None,
        email: str | None = None,
    ) -> httpx.Response:
        """POST `cancel`, `reconcile` or `mark-failed` (as the approver unless `email`)."""
        h = as_user(self.client, email or self.approver)
        r = self.client.post(
            f"{deployments(self.slug)}/{deployment_id}/{action}",
            json=dict(body) if body is not None else None,
            headers=h,
        )
        self.client.cookies.clear()
        return r

    def apply(self, plan: Mapping[str, Any], *, complete: bool = True) -> dict[str, Any]:
        """Lease the apply job, record the step (intent, then complete) and post a succeeded
        receipt; the lease body."""
        leased = lease_kind(self.client, self.token, "apply")
        assert checkpoint(self.client, self.token, leased, 1, "intent").status_code == 200
        if complete:
            done = checkpoint(self.client, self.token, leased, 2, "complete", {"ok": True})
            assert done.status_code == 200, done.text
        value = receipt_value(self.tenant, plan["target_id"], plan["artifact_digest"])
        r = job_result(self.client, self.token, leased, status="succeeded", value=value)
        assert r.status_code == 200, r.text
        return leased
