"""Connection discovery runs on the worker (ARCH M2, issue #144, DECISIONS B-2971).

`POST .../connections/{id}/discover` checks permission and adapter readiness, queues one
`connection.discover` job (no run) in the caller's client and answers 202; the API process never
resolves the secret or calls the adapter. The job runs as its requester, re-derived from the
database, with `connection:test` checked again, and records the `connection_discoveries` row whose
id the payload allocated; `GET .../discovery-jobs/{job_id}` reports `queued | running | done |
failed` and that row. Jobs are executed here by `conntests.execute_job` with
`connections.run_discover_job`, on a job leased only if it is still queued.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies, row values
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.testservers import RestTestServer
from shifde.connections import repository as connections_repo
from shifde.connections import service as connections
from shifde.core import db
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.conntests import discover_status, execute_job, run_discover
from tests.connections.routes import (
    REST_TOKEN,
    activate,
    as_user,
    connection,
    limited_user,
    org,
    rest_body,
    secret,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People


def _q(owner_db: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # Any: row values
    with owner_db.session() as o:
        return [tuple(r) for r in o.execute(text(sql), params).all()]


def _jobs(owner_db: Database, cid: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT id, tenant_id, kind, run_id, status, payload, dedupe_key, last_error "
        "FROM shifde.jobs WHERE kind = 'connection.discover' AND payload->>'connection_id' = :c "
        "ORDER BY created_at, id",
        c=cid,
    )


def _found(owner_db: Database, cid: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT id, kind, discovered_by FROM shifde.connection_discoveries "
        "WHERE connection_id = :c ORDER BY id",
        c=cid,
    )


def _secret_reads(owner_db: Database) -> int:
    return int(_q(owner_db, "SELECT count(*) FROM shifde.secret_access_log")[0][0])


def _audits(owner_db: Database, cid: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT actor_id, outcome, details FROM shifde.audit_events "
        "WHERE action = 'connection.discovered' AND target_id = :c ORDER BY occurred_at, id",
        c=cid,
    )


def _openapi_calls(server: RestTestServer) -> int:
    return sum(1 for r in server.requests if r["path"] == "/openapi.json")


def _post(client: TestClient, h: dict[str, str], A: str, cid: str) -> Any:  # Any: JSON body
    r = client.post(f"{A}/connections/{cid}/discover", headers=h)
    assert r.status_code == 202, r.text
    return r.json()


def _execute(client: TestClient, tenant: UUID, job_id: str) -> bool:
    app: Any = client.app  # Any: the FastAPI app behind the test client
    return execute_job(
        app.state.db,
        app.state.integration,
        tenant,
        UUID(job_id),
        kind=connections.DISCOVER_JOB_KIND,
        runner=connections.run_discover_job,
    )


def _rest(
    client: TestClient,
    h: dict[str, str],
    A: str,
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    server: RestTestServer,
    **config: str,
) -> str:
    sid = secret(app_db, settings, tenant, REST_TOKEN)
    body = rest_body(server, sid)
    body["config"] = {**body["config"], **config}
    return str(connection(client, h, body, A)["id"])


def test_the_post_queues_a_job_and_never_calls_the_adapter(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    activate(client, h, cid, A)
    calls, reads = len(rest_server.requests), _secret_reads(owner_db)
    job = _post(client, h, A, cid)
    assert len(rest_server.requests) == calls  # no request reached the server during the POST
    assert _secret_reads(owner_db) == reads  # and the secret was not resolved
    assert set(job) == {"id", "connection_id", "status", "result", "error", "created_at"}
    assert job["connection_id"] == cid and job["status"] == "queued"
    assert job["result"] is None and job["error"] is None and job["created_at"]
    [(jid, tenant, kind, run_id, status, payload, dedupe, _)] = _jobs(owner_db, cid)
    assert (str(jid), tenant, kind, run_id, status) == (
        job["id"],
        world.a.tenant,
        connections.DISCOVER_JOB_KIND,
        None,
        "queued",
    )
    assert kind == "connection.discover" and dedupe == f"connection-discover:{cid}"
    assert set(payload) == {
        "connection_id",
        "discovery_id",
        "principal_id",
        "principal_type",
        "api_key_id",
    }
    assert payload["connection_id"] == cid and payload["principal_type"] == "user"
    assert payload["api_key_id"] is None
    assert _found(owner_db, cid) == []  # nothing recorded until the job runs
    assert discover_status(client, h, A, cid, job["id"])["status"] == "queued"


def test_the_job_records_the_discovery_and_latest_returns_it(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    activate(client, h, cid, A)
    job = _post(client, h, A, cid)
    assert _execute(client, world.a.tenant, job["id"])
    assert _openapi_calls(rest_server) == 1  # the worker called it
    out = discover_status(client, h, A, cid, job["id"])
    assert out["status"] == "done" and out["error"] is None
    result = out["result"]
    assert result["kind"] == "openapi"
    assert {"method": "GET", "path": "/echo", "summary": "echo"} in result["payload"]["paths"]
    [(_, _, _, _, status, payload, _, _)] = _jobs(owner_db, cid)
    assert status == "done" and result["id"] == payload["discovery_id"]
    latest = client.get(f"{A}/connections/{cid}/discoveries/latest", headers=h)
    assert latest.status_code == 200 and latest.json() == result
    [(_, _, discovered_by)] = _found(owner_db, cid)
    assert str(discovered_by) == payload["principal_id"]  # the requester, re-derived
    [(actor, outcome, details)] = _audits(owner_db, cid)
    assert (str(actor), outcome, details["kind"]) == (payload["principal_id"], "success", "openapi")
    # the inline `discover()` (the ontology mapping check's path) gives the same document
    app: Any = client.app  # Any: the FastAPI app behind the test client
    with app.state.db.session() as session:
        db.set_tenant(session, world.a.tenant)
        scope = connections._job_scope(session, world.a.tenant, payload, "inline-discover")
        inline = connections.ConnectionService(scope, app.state.integration).discover(UUID(cid))
    assert (inline.kind, inline.payload) == (result["kind"], result["payload"])
    assert str(inline.id) != result["id"]


def test_two_posts_share_one_job_and_one_discovery(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    first, second = _post(client, h, A, cid), _post(client, h, A, cid)
    assert first["id"] == second["id"]
    assert [r[4] for r in _jobs(owner_db, cid)] == ["queued"]  # exactly one active job
    assert _execute(client, world.a.tenant, first["id"])
    assert not _execute(client, world.a.tenant, second["id"])  # no longer queued: not run again
    assert len(_found(owner_db, cid)) == 1
    third = _post(client, h, A, cid)  # once it ended, a new discovery is a new job
    assert third["id"] != first["id"] and third["status"] == "queued"


def test_synchronous_refusals_queue_nothing(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A = world.a, org(world.a.slug)
    owner = as_user(client, people.owner_a)
    planned = {
        "name": "planned",
        "connection_type": "data_platform",
        "adapter_key": "snowflake",
        "config": {"account": "a"},
    }
    pid = str(connection(client, owner, planned, A)["id"])
    r = client.post(f"{A}/connections/{pid}/discover", headers=owner)
    assert r.status_code == 422 and r.json()["code"] == "adapter_not_implemented", r.text
    assert _jobs(owner_db, pid) == []
    cid = _rest(client, owner, A, app_db, settings, a.tenant, rest_server)
    reader, _ = limited_user(owner_db, a.tenant, ["connection:read"])
    r = client.post(f"{A}/connections/{cid}/discover", headers=as_user(client, reader))
    assert r.status_code == 403, r.text  # readable, not testable
    nobody, _ = limited_user(owner_db, a.tenant, [], label="nobody")
    r = client.post(f"{A}/connections/{cid}/discover", headers=as_user(client, nobody))
    assert r.status_code == 404, r.text  # not even readable
    unknown = str(uuid7())
    owner = as_user(client, people.owner_a)
    r = client.post(f"{A}/connections/{unknown}/discover", headers=owner)
    assert r.status_code == 404 and r.json()["detail"] == "connection not found", r.text
    assert _jobs(owner_db, cid) == [] and _jobs(owner_db, unknown) == []


def test_a_permission_removed_before_the_job_runs_fails_it_and_records_nothing(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Jobs carry no permissions: `connection:test` is checked again under the re-derived
    requester."""
    a, A = world.a, org(world.a.slug)
    owner = as_user(client, people.owner_a)
    cid = _rest(client, owner, A, app_db, settings, a.tenant, rest_server)
    email, uid = limited_user(owner_db, a.tenant, ["connection:read", "connection:test"])
    h = as_user(client, email)
    job = _post(client, h, A, cid)
    with owner_db.session() as o:
        o.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
        o.execute(
            text(
                "DELETE FROM shifde.role_permissions WHERE permission_key = 'connection:test' "
                "AND role_id IN (SELECT role_id FROM shifde.role_bindings WHERE subject_id = :u)"
            ),
            {"u": uid},
        )
        o.commit()
    calls = len(rest_server.requests)
    assert _execute(client, a.tenant, job["id"])
    h = as_user(client, email)
    out = discover_status(client, h, A, cid, job["id"])
    assert out == {**out, "status": "failed", "result": None, "error": "forbidden"}
    assert [(r[4], r[7]) for r in _jobs(owner_db, cid)] == [("dead", "forbidden")]
    assert _found(owner_db, cid) == [] and _audits(owner_db, cid) == []
    assert len(rest_server.requests) == calls


def test_an_adapter_failure_fails_the_job_with_its_code_and_audits_it(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """The code the inline route answered as an HTTP error (`failure_to_error`): here a missing
    OpenAPI document, a `capability` failure, is `validation_error`; the failure audit stays."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(
        client, h, A, app_db, settings, world.a.tenant, rest_server, openapi_path="/missing.json"
    )
    out = run_discover(client, h, A, cid)
    assert (out["status"], out["error"], out["result"]) == ("failed", "validation_error", None)
    assert [(r[4], r[7]) for r in _jobs(owner_db, cid)] == [("dead", "validation_error")]
    assert _found(owner_db, cid) == []
    [(_, outcome, details)] = _audits(owner_db, cid)
    assert (outcome, details["error_class"]) == ("failure", "capability")


def test_re_running_an_executed_job_records_no_second_discovery(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    out = run_discover(client, h, A, cid)
    assert out["status"] == "done"
    app: Any = client.app  # Any: the FastAPI app behind the test client
    again = connections.run_discover_job(
        app.state.db, app.state.integration, world.a.tenant, UUID(out["id"])
    )
    assert again is None  # already recorded: success, nothing run
    assert [str(r[0]) for r in _found(owner_db, cid)] == [out["result"]["id"]]
    assert _openapi_calls(rest_server) == 1  # the adapter was called once
    assert len(_audits(owner_db, cid)) == 1


def test_a_unique_violation_on_the_discovery_id_counts_as_recorded(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another attempt of the same job recorded the discovery after this one's idempotency check:
    the insert hits the primary key, the job ends `done`, and no second audit event is written."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    out = run_discover(client, h, A, cid)
    assert out["status"] == "done"
    with owner_db.session() as o:  # the job is attempted again
        o.execute(
            text("UPDATE shifde.jobs SET status = 'queued', finished_at = NULL WHERE id = :j"),
            {"j": out["id"]},
        )
        o.commit()
    real = connections_repo.get_discovery
    checks: list[UUID] = []

    def first_misses(session: Any, discovery_id: UUID, *, tenant_id: UUID) -> Any:
        checks.append(discovery_id)
        return None if len(checks) == 1 else real(session, discovery_id, tenant_id=tenant_id)

    monkeypatch.setattr(connections_repo, "get_discovery", first_misses)
    assert _execute(client, world.a.tenant, out["id"])
    monkeypatch.undo()
    assert len(checks) == 2  # the idempotency check missed; the read after the violation found it
    assert [(r[4], r[7]) for r in _jobs(owner_db, cid)] == [("done", None)]
    assert [str(r[0]) for r in _found(owner_db, cid)] == [out["result"]["id"]]
    assert len(_audits(owner_db, cid)) == 1
    assert discover_status(client, h, A, cid, out["id"]) == out


def test_a_job_closed_without_a_result_reads_as_worker_lost(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """The reaper closes an expired run-less job `done` with `stale lease`: the status route
    reports `failed` / `worker_lost`, never the raw `last_error` text."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    job = _post(client, h, A, cid)
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'gone', "
                "lease_expires_at = now() - interval '1 second' WHERE id = :j"
            ),
            {"j": job["id"]},
        )
        o.commit()
    assert discover_status(client, h, A, cid, job["id"])["status"] == "running"
    with owner_db.session() as o:
        o.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
        o.commit()
    assert [(r[4], r[7]) for r in _jobs(owner_db, cid)] == [("done", "stale lease")]
    out = discover_status(client, h, A, cid, job["id"])
    assert (out["status"], out["error"], out["result"]) == ("failed", "worker_lost", None)


def test_a_job_that_cannot_be_read_back_answers_409_busy_after_three_attempts(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    reads: list[str] = []

    def miss(session: Any, *, tenant_id: UUID, kind: str, dedupe_key: str) -> None:
        reads.append(dedupe_key)

    monkeypatch.setattr(connections_repo, "_active_test_job", miss)
    r = client.post(f"{A}/connections/{cid}/discover", headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "connection_discovery_busy"
    assert reads == [f"connection-discover:{cid}"] * 3
    assert _jobs(owner_db, cid) == []  # rolled back: nothing queued
    monkeypatch.undo()
    job = _post(client, h, A, cid)  # a retry queues normally
    assert [r[4] for r in _jobs(owner_db, cid)] == ["queued"] and job["status"] == "queued"


def test_an_unknown_job_id_of_the_client_is_not_found(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    missing = client.get(f"{A}/connections/{cid}/discovery-jobs/{uuid7()}", headers=h)
    assert missing.status_code == 404 and missing.json()["detail"] == "discovery job not found"
    app: Any = client.app  # Any: the FastAPI app behind the test client
    test_job = client.post(f"{A}/connections/{cid}/test", headers=h).json()["id"]
    # a test job is no discover job, on the route and on the worker side
    other = client.get(f"{A}/connections/{cid}/discovery-jobs/{test_job}", headers=h)
    assert other.status_code == 404
    assert (
        connections.run_discover_job(
            app.state.db, app.state.integration, world.a.tenant, UUID(test_job)
        )
        == "not_found"
    )
