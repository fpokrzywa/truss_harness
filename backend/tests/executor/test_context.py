"""`GET /api/executor/v1/jobs/{id}/context` (B-2688): the target (host = its address), the sealed
plan, the deployment's receipt and the owned native resources of a leased job, the plan and the
deployment each with its own row's organization (issue #155); 409 `lease_lost` after the lease
ends; never a credential, the claim or the address key."""

from __future__ import annotations

from pathlib import Path
from typing import Any  # Any: JSON bodies

import httpx
from fastapi.testclient import TestClient

from shifde.core.db import Database
from tests.connections.routes import as_user
from tests.delivery.routes import (
    Flow,
    bearer,
    expire,
    job_url,
    lease_kind,
    make_due,
    proof,
    targets,
)
from tests.executor.support import ask_plan, credentials_file, fake_platform, make_runner

FORBIDDEN_KEYS = {"token", "secret", "claim_id", "address_key"}


def _context(client: TestClient, token: str, leased: dict[str, Any]) -> httpx.Response:
    return client.get(
        job_url(leased["job"]["id"], "context"), params=proof(leased), headers=bearer(token)
    )


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_apply_and_observe_contexts(flow: Flow, tmp_path: Path, owner_db: Database) -> None:
    fake = fake_platform()
    target = flow.target()
    creds = credentials_file(tmp_path, target["address"], tenant=flow.tenant)
    runner = make_runner(flow.client, flow.token, creds, fake)

    # first deployment of sales-app: applied by the runner, so the app is owned
    plan = ask_plan(flow, target["id"])
    assert runner.run_once()
    first = flow.approve(flow.get_plan(plan["id"]))
    assert runner.run_once()
    receipt = flow.get(first["deployment_id"])["receipt"]
    native_id = receipt["resources"][0]["native_id"]

    # the observe job of the first deployment carries its receipt
    make_due(owner_db, first["deployment_id"])
    leased = lease_kind(flow.client, flow.token, "observe")
    r = _context(flow.client, flow.token, leased)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job"]["id"] == leased["job"]["id"]
    assert body["deployment"] == {
        "id": first["deployment_id"],
        "tenant_id": str(flow.tenant),
        "status": "observing",
        "receipt": receipt,
    }
    assert not _keys(body) & FORBIDDEN_KEYS
    expire(owner_db, leased["job"]["id"])
    lost = _context(flow.client, flow.token, leased)
    assert lost.status_code == 409 and lost.json()["code"] == "lease_lost", lost.text

    # a redeployment: its apply job sees the sealed plan and the owned app
    plan = ask_plan(flow, target["id"])
    while runner.run_once():  # the re-queued observe job and the plan job
        pass
    second = flow.approve(flow.get_plan(plan["id"]))
    leased = lease_kind(flow.client, flow.token, "apply")
    r = _context(flow.client, flow.token, leased)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == {
        "id": target["id"],
        "tenant_id": str(flow.tenant),
        "revision": target["revision"],
        "environment": "dev",
        "host": target["address"],
        "bindings": target["bindings"],
    }
    for key in ("id", "artifact_id", "artifact_digest", "steps", "snapshot", "digest"):
        assert body["plan"][key] == second[key], key
    assert body["plan"]["status"] == "approved"
    assert body["plan"]["tenant_id"] == str(flow.tenant)
    assert body["plan"]["toolchain_digest"] == second["toolchain_digest"]
    assert body["plan"]["expires_at"] is not None
    assert body["deployment"] == {
        "id": second["deployment_id"],
        "tenant_id": str(flow.tenant),
        "status": "leased",
        "receipt": None,
    }
    assert body["owned_resources"] == [{"resource_type": "databricks.app", "native_id": native_id}]
    assert not _keys(body) & FORBIDDEN_KEYS
    assert "credential_ref" not in body["target"]


def test_a_plan_job_context_before_sealing(flow: Flow) -> None:
    target = flow.target()
    plan = ask_plan(flow, target["id"])
    leased = lease_kind(flow.client, flow.token, "plan")
    r = _context(flow.client, flow.token, leased)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["id"] == plan["id"] and body["plan"]["status"] == "planning"
    assert body["plan"]["tenant_id"] == str(flow.tenant)
    for key in ("steps", "snapshot", "adapter_version", "toolchain_digest", "digest"):
        assert body["plan"][key] is None
    assert body["plan"]["expires_at"] is None
    assert body["deployment"] is None and body["owned_resources"] == []


def test_an_archived_target_is_unavailable(flow: Flow) -> None:
    target = flow.target()
    ask_plan(flow, target["id"])
    leased = lease_kind(flow.client, flow.token, "plan")
    h = as_user(flow.client, flow.requester)
    r = flow.client.post(f"{targets(flow.slug)}/{target['id']}/archive", headers=h)
    flow.client.cookies.clear()
    assert r.status_code == 200, r.text
    r = _context(flow.client, flow.token, leased)
    assert r.status_code == 409 and r.json()["code"] == "target_unavailable", r.text
