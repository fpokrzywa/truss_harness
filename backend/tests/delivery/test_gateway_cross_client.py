"""Executor gateway routes, organization A against organization B (B-1019, B-2669; isolation
review of SP2-3, finding 1, issue #64 item 1).

Every route of `delivery.gateway_api.router` has a test here (`GATEWAY_ROUTE_TESTS`, matched
against the router). Each organization has an active executor with a token and a queued job. B's
lease never returns A's job; B's token on A's job id, even with A's current `lease_id` and
`fence`, gets exactly the 404 an unknown job id gets, and A's job and journal rows stay as they
were, read as the schema owner. B's lease call sweeps only B's expired leases. User sessions and
`shf_` API keys get 401 on every gateway route.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery import gateway_api
from shifde.identity import apikeys
from tests.connections.routes import as_user
from tests.delivery.conftest import ExecutorSide, make_executor
from tests.delivery.routes import (
    LEASE,
    WHOAMI,
    bearer,
    enqueue,
    expire,
    job_row,
    job_url,
    journal_rows,
    lease,
    proof,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People

_VARYING = {"instance", "request_id"}
DIGEST = "c" * 64

# ------------------------------------------------------------------ route table
JOB = "/api/executor/v1/jobs/{job_id}"
GATEWAY_ROUTE_TESTS = {
    ("GET", "/api/executor/v1/whoami"): "test_whoami_answers_for_each_organizations_executor",
    ("POST", "/api/executor/v1/lease"): "test_bs_lease_never_returns_as_job",
    ("POST", JOB + "/heartbeat"): "test_b_cannot_reach_as_job",
    ("GET", JOB + "/artifact"): "test_b_cannot_reach_as_job",
    ("POST", JOB + "/credential-grant"): "test_b_cannot_reach_as_job",
    ("POST", JOB + "/checkpoint"): "test_b_cannot_reach_as_job",
    ("POST", JOB + "/result"): "test_b_cannot_reach_as_job",
}


def test_every_gateway_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in gateway_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(GATEWAY_ROUTE_TESTS)
    assert len(routes) == 7
    assert all(name in globals() for name in GATEWAY_ROUTE_TESTS.values())


@dataclass(frozen=True)
class Two:
    a: ExecutorSide
    b: ExecutorSide
    job_a: UUID
    job_b: UUID


@pytest.fixture
def two(client: TestClient, world: World, people: People, app_db: Database) -> Two:
    a = make_executor(client, world.a, people.owner_a)
    b = make_executor(client, world.b, people.owner_b)
    job_a = enqueue(app_db, a.tenant, "observe", deployment_id=uuid7())
    job_b = enqueue(app_db, b.tenant, "observe", deployment_id=uuid7(), run_after=_later())
    return Two(a, b, job_a, job_b)


def _later() -> Any:  # Any: a datetime
    return utcnow() + timedelta(hours=1)


def _due_now(owner_db: Database, job: UUID) -> None:
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.delivery_jobs SET run_after = now() WHERE id = :j"), {"j": job}
        )
        o.commit()


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


def _calls(leased: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(method, action, request options) of every per-job route, with A's lease proof."""
    p = proof(leased)
    return [
        ("POST", "heartbeat", {"json": p}),
        ("GET", "artifact", {"params": p}),
        ("POST", "credential-grant", {"json": p}),
        (
            "POST",
            "checkpoint",
            {
                "json": {
                    **p,
                    "sequence": 9,
                    "step_key": "deploy_app",
                    "state": "complete",
                    "input_digest": DIGEST,
                    "receipt": {"forged": True},
                }
            },
        ),
        ("POST", "result", {"json": {**p, "status": "failed", "error_code": "execution_failed"}}),
    ]


def test_bs_lease_never_returns_as_job(client: TestClient, two: Two, owner_db: Database) -> None:
    # B has no due job of its own: 204, A's queued job untouched
    assert lease(client, two.b.token).status_code == 204
    assert job_row(owner_db, two.job_a)["status"] == "queued"
    _due_now(owner_db, two.job_b)
    r = lease(client, two.b.token)
    assert r.status_code == 200 and r.json()["job"]["id"] == str(two.job_b), r.text
    assert lease(client, two.b.token).status_code == 204
    assert job_row(owner_db, two.job_a)["status"] == "queued"
    r = lease(client, two.a.token)
    assert r.status_code == 200 and r.json()["job"]["id"] == str(two.job_a), r.text


def test_b_cannot_reach_as_job(client: TestClient, two: Two, owner_db: Database) -> None:
    r = lease(client, two.a.token)
    assert r.status_code == 200, r.text
    leased = r.json()
    intent = {
        **proof(leased),
        "sequence": 1,
        "step_key": "deploy_app",
        "state": "intent",
        "input_digest": DIGEST,
    }
    r = client.post(job_url(two.job_a, "checkpoint"), json=intent, headers=bearer(two.a.token))
    assert r.status_code == 200, r.text
    before = (dict(job_row(owner_db, two.job_a)), journal_rows(owner_db, two.a.tenant))
    for method, action, options in _calls(leased):
        missing = client.request(
            method, job_url(uuid7(), action), headers=bearer(two.b.token), **options
        )
        r = client.request(
            method, job_url(two.job_a, action), headers=bearer(two.b.token), **options
        )
        _same_404(r, missing)
    after = (dict(job_row(owner_db, two.job_a)), journal_rows(owner_db, two.a.tenant))
    assert after == before
    # A's own executor still holds the lease
    r = client.post(
        job_url(two.job_a, "heartbeat"), json=proof(leased), headers=bearer(two.a.token)
    )
    assert r.status_code == 200, r.text


def test_bs_lease_does_not_sweep_as_expired_job(
    client: TestClient, two: Two, owner_db: Database
) -> None:
    assert lease(client, two.a.token).status_code == 200
    expire(owner_db, two.job_a)
    before = dict(job_row(owner_db, two.job_a))
    assert lease(client, two.b.token).status_code == 204
    assert dict(job_row(owner_db, two.job_a)) == before
    assert before["status"] == "leased"
    # A's own lease call sweeps it (re-queued, then leased again)
    r = lease(client, two.a.token)
    assert r.status_code == 200 and r.json()["fence"] == 2, r.text


def _owner_id(owner_db: Database, email: str) -> UUID:
    with owner_db.session() as o:
        uid: UUID = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
    return uid


def test_user_credentials_get_401_on_every_gateway_route(
    client: TestClient,
    world: World,
    people: People,
    two: Two,
    owner_db: Database,
    app_db: Database,
    settings: Settings,
) -> None:
    r = lease(client, two.a.token)
    assert r.status_code == 200, r.text
    leased = r.json()
    owner = _owner_id(owner_db, people.owner_a)
    with app_session(app_db, world.a.tenant, owner) as s:
        _, key = apikeys.mint(
            s,
            settings,
            tenant_id=world.a.tenant,
            name="owner key",
            principal_type="user",
            principal_id=owner,
            scopes=["delivery:executor:read"],
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=owner,
        )
        s.commit()
    session_headers = as_user(client, people.owner_a)
    calls = [
        ("POST", LEASE, {"json": {"request_id": str(uuid7()), "kinds": ["observe"]}}),
        *(
            (method, job_url(two.job_a, action), options)
            for method, action, options in _calls(leased)
        ),
    ]
    for name, headers in (("session", session_headers), ("api key", bearer(key))):
        for method, url, options in calls:
            r = client.request(method, url, headers=headers, **options)
            assert r.status_code == 401 and r.json()["code"] == "invalid_credentials", (
                name,
                url,
                r.text,
            )
    client.cookies.clear()
    assert dict(job_row(owner_db, two.job_a))["fence"] == 1


def test_whoami_answers_for_each_organizations_executor(client: TestClient, two: Two) -> None:
    for side in (two.a, two.b):
        r = client.get(WHOAMI, headers=bearer(side.token))
        assert r.status_code == 200 and r.json()["executor_id"] == side.executor_id, r.text
