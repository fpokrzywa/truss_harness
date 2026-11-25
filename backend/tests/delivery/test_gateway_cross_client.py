"""Executor gateway routes, organization A against organization B (B-1019, B-2669; isolation
review of SP2-3, finding 1, issue #64 item 1; issue #74).

Every route of `delivery.gateway_api.router` has a test here (`GATEWAY_ROUTE_TESTS`, matched
against the router). Each organization has an active executor with a token and a queued job. B's
lease never returns A's job; B's token on A's job id, even with A's current `lease_id` and
`fence`, gets exactly the 404 an unknown job id gets, and A's job and journal rows stay as they
were, read as the schema owner. B's lease call sweeps only B's expired leases. User sessions and
`shf_` API keys get 401 on every gateway route. With both organizations owning native resources
under the same application key, A's job context lists only A's.
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
from shifde.delivery import gateway_api, repository
from shifde.identity import apikeys
from tests.connections.routes import as_user, system_role_user
from tests.delivery.conftest import ExecutorSide, make_executor
from tests.delivery.routes import (
    LEASE,
    WHOAMI,
    Flow,
    bearer,
    checkpoint,
    deployment_row,
    enqueue,
    enqueue_on_target,
    expire,
    job_row,
    job_url,
    journal_rows,
    lease,
    lease_kind,
    make_deployment,
    owner_sql,
    proof,
    register,
    target_body,
    targets,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws_slug

_VARYING = {"instance", "request_id"}
DIGEST = "c" * 64

# ------------------------------------------------------------------ route table
JOB = "/api/executor/v1/jobs/{job_id}"
GATEWAY_ROUTE_TESTS = {
    ("GET", "/api/executor/v1/whoami"): "test_whoami_answers_for_each_organizations_executor",
    ("POST", "/api/executor/v1/lease"): "test_bs_lease_never_returns_as_job",
    ("POST", JOB + "/heartbeat"): "test_b_cannot_reach_as_job",
    ("GET", JOB + "/artifact"): "test_b_cannot_reach_as_job",
    ("GET", JOB + "/context"): "test_b_cannot_reach_as_job",
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
    assert len(routes) == 8
    assert all(name in globals() for name in GATEWAY_ROUTE_TESTS.values())


@dataclass(frozen=True)
class Two:
    a: ExecutorSide
    b: ExecutorSide
    job_a: UUID
    job_b: UUID


@pytest.fixture
def two(
    client: TestClient, world: World, people: People, app_db: Database, owner_db: Database
) -> Two:
    a = make_executor(client, world.a, people.owner_a)
    b = make_executor(client, world.b, people.owner_b)
    job_a = enqueue_on_target(app_db, a.tenant, "observe", make_deployment(owner_db, a.tenant))
    job_b = enqueue(
        app_db,
        b.tenant,
        "observe",
        deployment_id=make_deployment(owner_db, b.tenant),
        run_after=_later(),
    )
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
        ("GET", "context", {"params": p}),
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


def test_bs_intent_on_as_archived_target_gets_the_unknown_jobs_404(
    client: TestClient, people: People, two: Two, owner_db: Database
) -> None:
    """The state of A's target is no oracle: B's intent with A's lease proof gets the 404 an
    unknown job id gets, never A's 409 `target_unavailable`."""
    leased = lease(client, two.a.token).json()
    assert leased["job"]["id"] == str(two.job_a)
    target = job_row(owner_db, two.job_a)["target_id"]
    h = as_user(client, people.owner_a)
    r = client.post(f"{targets(two.a.slug)}/{target}/archive", headers=h)
    client.cookies.clear()
    assert r.status_code == 200, r.text
    before = dict(job_row(owner_db, two.job_a))
    missing = checkpoint(client, two.b.token, {**leased, "job": {"id": uuid7()}}, 1, "intent")
    _same_404(checkpoint(client, two.b.token, leased, 1, "intent"), missing)
    own = checkpoint(client, two.a.token, leased, 1, "intent")
    assert own.status_code == 409 and own.json()["code"] == "target_unavailable", own.text
    assert dict(job_row(owner_db, two.job_a)) == before
    assert journal_rows(owner_db, two.a.tenant) == journal_rows(owner_db, two.b.tenant) == []


def test_as_intent_is_refused_once_b_registered_the_freed_address(
    client: TestClient, world: World, people: People, flow: Flow, owner_db: Database
) -> None:
    """A holds a leased apply and archives its target; B registers the same address and applies
    there. A's new intent is refused, B's is recorded, and neither journal gains the other's row."""
    target = flow.target()
    _, deployment_a = flow.deployment(target["id"])
    leased_a = lease_kind(client, flow.token, "apply")
    assert leased_a["job"]["deployment_id"] == deployment_a
    flow.archive(target["id"])
    b = Flow(
        client,
        world.b.slug,
        ws_slug(world.b),
        world.b.tenant,
        people.owner_b,
        system_role_user(owner_db, world.b.tenant, "tenant_administrator"),
        make_executor(client, world.b, people.owner_b).token,
        owner_db,
    )
    body = target_body(b.workspace, target["address"])
    taken = register(client, as_user(client, b.requester), b.slug, body)
    client.cookies.clear()
    assert taken["address"] == target["address"]
    _, deployment_b = b.deployment(taken["id"])
    leased_b = lease_kind(client, b.token, "apply")
    assert leased_b["job"]["deployment_id"] == deployment_b
    r = checkpoint(client, flow.token, leased_a, 1, "intent")
    assert r.status_code == 409 and r.json()["code"] == "target_unavailable", r.text
    r = checkpoint(client, b.token, leased_b, 1, "intent")
    assert r.status_code == 200, r.text
    assert journal_rows(owner_db, flow.tenant) == []
    (row,) = journal_rows(owner_db, b.tenant)
    assert row[:2] == (b.tenant, UUID(deployment_b))
    assert deployment_row(owner_db, deployment_a)["status"] == "leased"
    assert deployment_row(owner_db, deployment_b)["status"] == "applying"


def test_b_cannot_replay_as_request_id(client: TestClient, two: Two, owner_db: Database) -> None:
    """Isolation review 1 of SP2-4, finding 3: a request id names a lease only in its own
    organization. B has a due job, so an organization-unscoped replay lookup would answer B
    with A's lease (or nothing) instead of leasing B's own job (isolation review 2, finding 1)."""
    request_id = uuid7()
    r = lease(client, two.a.token, request_id=request_id)
    assert r.status_code == 200 and r.json()["job"]["id"] == str(two.job_a), r.text
    before = dict(job_row(owner_db, two.job_a))
    _due_now(owner_db, two.job_b)
    b = lease(client, two.b.token, request_id=request_id)
    assert b.status_code == 200 and b.json()["job"]["id"] == str(two.job_b), b.text
    assert dict(job_row(owner_db, two.job_a)) == before
    assert job_row(owner_db, two.job_b)["status"] == "leased"
    # A's own replay still answers its lease
    again = lease(client, two.a.token, request_id=request_id)
    assert again.status_code == 200 and again.json() == r.json(), again.text


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
        assert r.json()["tenant_id"] == str(side.tenant)


def _native_rows(owner_db: Database, tenant: UUID) -> list[tuple[Any, ...]]:
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT target_id, application_key, resource_type, native_id, deployment_id "
                "FROM shifde.delivery_native_resources WHERE tenant_id = :t ORDER BY native_id"
            ),
            {"t": tenant},
        ).all()
    return [tuple(r) for r in rows]


def test_as_context_lists_only_as_resources_under_a_shared_application_key(
    client: TestClient, world: World, people: People, app_db: Database, owner_db: Database
) -> None:
    """A and B each own native resources under `helper-app` (`make_deployment`'s key) on their own
    target, made active; A's plan job context lists exactly A's, and B's rows stay as they were."""
    a = make_executor(client, world.a, people.owner_a)
    plans: dict[UUID, tuple[UUID, UUID]] = {}
    for side in (a.tenant, world.b.tenant):
        deployment = deployment_row(owner_db, make_deployment(owner_db, side))
        owner_sql(  # activate the target under a claim of its own organization
            owner_db,
            "WITH c AS (INSERT INTO shifde.delivery_platform_claims (id, tenant_id, platform_kind, "
            "address_key, created_by) VALUES (:c, :t, 'databricks', :k, :u) RETURNING id) "
            "UPDATE shifde.delivery_targets SET status = 'active', claim_id = (SELECT id FROM c) "
            "WHERE id = :g",
            c=uuid7(),
            t=side,
            k=uuid7().hex * 2,
            u=uuid7(),
            g=deployment["target_id"],
        )
        with app_session(app_db, side) as s:
            for resource_type in ("databricks_app", "workspace_directory"):
                repository.insert_native_resource(
                    s,
                    {
                        "tenant_id": side,
                        "target_id": deployment["target_id"],
                        "application_key": deployment["application_key"],
                        "resource_type": resource_type,
                        "native_id": f"{resource_type}-{uuid7().hex[-12:]}",
                        "deployment_id": deployment["id"],
                    },
                )
            s.commit()
        plans[side] = (deployment["plan_id"], deployment["target_id"])
    b_before = _native_rows(owner_db, world.b.tenant)
    a_rows = _native_rows(owner_db, a.tenant)
    plan_a, target_a = plans[a.tenant]
    job = enqueue(app_db, a.tenant, "plan", plan_id=plan_a, target_id=target_a)
    r = lease(client, a.token, kinds=("plan",))
    assert r.status_code == 200 and r.json()["job"]["id"] == str(job), r.text
    r = client.get(job_url(job, "context"), params=proof(r.json()), headers=bearer(a.token))
    assert r.status_code == 200, r.text
    owned = {(o["resource_type"], o["native_id"]) for o in r.json()["owned_resources"]}
    assert owned == {(row[2], row[3]) for row in a_rows}
    assert len(owned) == 2
    assert not owned & {(row[2], row[3]) for row in b_before}
    assert _native_rows(owner_db, world.b.tenant) == b_before
