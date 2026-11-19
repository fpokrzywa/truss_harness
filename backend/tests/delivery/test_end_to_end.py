"""End to end against the fake platform (design 7, issue 7; B-2688..B-2695): the API in-process and
the `shifde.executor` runner over it. A's requester asks for a plan, A's runner seals it, the
requester cannot approve it, a second tenant administrator does, and A's runner takes the
deployment through apply, observe and verify to `verified`. B's executor, with its own token and
credentials file, leases nothing of A's while A's jobs are due, and changes none of A's rows.
Nothing contacts a real Databricks endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any  # Any: rows
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from tests.connections.routes import as_user
from tests.delivery.conftest import make_executor
from tests.delivery.routes import Flow, make_due, plans
from tests.executor.support import (
    ask_plan,
    credentials_file,
    deploy_actions,
    fake_platform,
    make_runner,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def _rows(owner_db: Database, tenant: UUID) -> dict[str, list[dict[str, Any]]]:
    """A's jobs, plans and deployments, read as the schema owner."""
    with owner_db.session() as o:
        return {
            table: [
                dict(r)
                for r in o.execute(
                    # the table names are the literals below
                    text(f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY id"),  # nosec B608
                    {"t": tenant},
                ).mappings()
            ]
            for table in ("delivery_jobs", "delivery_plans", "delivery_deployments")
        }


def test_plan_approve_apply_observe_verify_against_the_fake(
    client: TestClient,
    flow: Flow,
    world: World,
    people: People,
    owner_db: Database,
    tmp_path: Path,
) -> None:
    fake = fake_platform()
    target = flow.target()
    runner_a = make_runner(
        client, flow.token, credentials_file(tmp_path, target["address"], name="a.json"), fake
    )
    b = make_executor(client, world.b, people.owner_b)
    b_target_host = "https://adb-organization-b.cloud.databricks.com"
    runner_b = make_runner(
        client, b.token, credentials_file(tmp_path, b_target_host, name="b.json"), fake
    )

    # plan: requested by A's requester, sealed by A's runner
    plan = ask_plan(flow, target["id"])
    assert runner_a.run_once()
    sealed = flow.get_plan(plan["id"])
    assert sealed["status"] == "sealed", sealed

    # the requester cannot approve their own plan; a second tenant administrator does
    h = as_user(client, flow.requester)
    r = client.post(
        f"{plans(flow.slug)}/{plan['id']}/approve",
        json={"plan_digest": sealed["digest"]},
        headers=h,
    )
    client.cookies.clear()
    assert r.status_code == 403 and r.json()["code"] == "self_approval_forbidden", r.text
    approved = flow.approve(sealed)
    deployment_id = approved["deployment_id"]
    assert approved["status"] == "approved" and deployment_id is not None

    # B's executor leases nothing of A's while A's apply job is due
    before = _rows(owner_db, flow.tenant)
    assert not runner_b.run_once()
    assert _rows(owner_db, flow.tenant) == before
    assert fake.actions == []

    # A's runner: apply, then observe and verify (each made due at once)
    assert runner_a.run_once()
    assert flow.get(deployment_id)["status"] == "observing"
    make_due(owner_db, deployment_id)
    before = _rows(owner_db, flow.tenant)
    assert not runner_b.run_once()  # still nothing of A's, with A's observe job due
    assert _rows(owner_db, flow.tenant) == before
    assert runner_a.run_once()
    assert flow.get(deployment_id)["status"] == "verifying"
    make_due(owner_db, deployment_id)
    assert runner_a.run_once()
    assert not runner_a.run_once()

    body = flow.get(deployment_id)
    assert body["status"] == "verified", body
    assert {c["name"]: c["status"] for c in body["verification"]["checks"]} == {
        "native-runtime": "passed",
        "runtime-warehouse": "passed",
    }
    assert [(s["step_key"], s["state"]) for s in body["journal"]] == [
        ("bundle-deploy", "complete"),
        ("bundle-run", "complete"),
    ]
    assert all(s["receipt"]["platform"] == "fake" for s in body["journal"])
    assert body["receipt"]["operation_handles"]["platform"] == "fake"
    assert (deploy_actions(fake, "deploy"), deploy_actions(fake, "run")) == (1, 1)
    assert len(fake.actions) == 2
