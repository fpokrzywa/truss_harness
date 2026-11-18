"""Connection tests run on the worker (ARCH M2, issue #10, DECISIONS B-2610).

`POST .../connections/{id}/test` checks permission and adapter readiness, queues one
`connection.test` job (no run) in the caller's client and answers 202; the API process never calls
the adapter. The job runs as its requester, re-derived from the database, with `connection:test`
checked again; `GET .../test-jobs/{job_id}` reports `queued | running | done | failed` and the
result. Jobs are executed here by `conntests.execute_job`, the worker's own job function on a job
leased only if it is still queued.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies, row values
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.testservers import RestTestServer
from shifde.connections import service as connections
from shifde.core.db import Database
from shifde.core.settings import Settings
from tests._support.conntests import execute_job, job_status, run_test
from tests.connections.routes import (
    REST_TOKEN,
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
        "FROM shifde.jobs WHERE payload->>'connection_id' = :c ORDER BY created_at, id",
        c=cid,
    )


def _tests(owner_db: Database, cid: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT id, outcome, tested_by FROM shifde.connection_tests WHERE connection_id = :c",
        c=cid,
    )


def _post(client: TestClient, h: dict[str, str], A: str, cid: str) -> Any:  # Any: JSON body
    r = client.post(f"{A}/connections/{cid}/test", headers=h)
    assert r.status_code == 202, r.text
    return r.json()


def _execute(client: TestClient, tenant: UUID, job_id: str) -> bool:
    app: Any = client.app  # Any: the FastAPI app behind the test client
    return execute_job(app.state.db, app.state.integration, tenant, UUID(job_id))


def _rest(
    client: TestClient,
    h: dict[str, str],
    A: str,
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    server: RestTestServer,
    token: str = REST_TOKEN,
) -> str:
    sid = secret(app_db, settings, tenant, token)
    return str(connection(client, h, rest_body(server, sid), A)["id"])


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
    calls = len(rest_server.requests)
    job = _post(client, h, A, cid)
    assert len(rest_server.requests) == calls  # no request reached the server during the POST
    assert job["connection_id"] == cid and job["status"] == "queued"
    assert job["result"] is None and job["error"] is None and job["created_at"]
    [(jid, tenant, kind, run_id, status, payload, dedupe, _)] = _jobs(owner_db, cid)
    assert (str(jid), tenant, kind, run_id, status) == (
        job["id"],
        world.a.tenant,
        connections.TEST_JOB_KIND,
        None,
        "queued",
    )
    assert dedupe == f"connection-test:{cid}"
    assert set(payload) == {
        "connection_id",
        "test_id",
        "principal_id",
        "principal_type",
        "api_key_id",
    }
    assert payload["connection_id"] == cid and payload["principal_type"] == "user"
    assert payload["api_key_id"] is None
    assert _tests(owner_db, cid) == []  # nothing recorded until the job runs
    assert job_status(client, h, A, cid, job["id"])["status"] == "queued"


def test_the_job_records_the_result_and_the_status_route_reports_it(
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
    before = client.get(f"{A}/connections/{cid}", headers=h).json()
    assert before["health_status"] == "unknown" and before["last_tested_at"] is None
    job = _post(client, h, A, cid)
    assert _execute(client, world.a.tenant, job["id"])
    assert any(r["path"] == "/health" for r in rest_server.requests)  # the worker called it
    out = job_status(client, h, A, cid, job["id"])
    assert out["status"] == "done" and out["error"] is None
    result = out["result"]
    assert result["outcome"] == "pass" and result["connection_id"] == cid
    [(_, _, _, _, status, payload, _, _)] = _jobs(owner_db, cid)
    assert status == "done" and result["id"] == payload["test_id"]
    listed = client.get(f"{A}/connections/{cid}/tests", headers=h).json()["items"]
    assert [t["id"] for t in listed] == [result["id"]]
    assert listed[0] == result
    after = client.get(f"{A}/connections/{cid}", headers=h).json()
    assert after["health_status"] == "healthy" and after["last_tested_at"] is not None
    [(_, _, tested_by)] = _tests(owner_db, cid)
    assert str(tested_by) == payload["principal_id"]  # the requester, re-derived
    actors = _q(
        owner_db,
        "SELECT actor_id, outcome FROM shifde.audit_events WHERE action = 'connection.tested' "
        "AND target_id = :c",
        c=cid,
    )
    assert [(str(a), o) for a, o in actors] == [(payload["principal_id"], "success")]


def test_a_failing_adapter_is_a_done_job_with_a_failed_test(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _rest(client, h, A, app_db, settings, world.a.tenant, rest_server, "wrong-token-xyz")
    out = run_test(client, h, A, cid)
    assert out["status"] == "done" and out["error"] is None
    assert out["result"]["outcome"] == "fail" and out["result"]["error_class"] == "auth"
    assert [r[1] for r in _tests(owner_db, cid)] == ["fail"]
    assert client.get(f"{A}/connections/{cid}", headers=h).json()["health_status"] == "unhealthy"


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
    requester; a requester who is no longer an active member is refused as well."""
    a, A = world.a, org(world.a.slug)
    owner = as_user(client, people.owner_a)
    cid = _rest(client, owner, A, app_db, settings, a.tenant, rest_server)
    email, uid = limited_user(owner_db, a.tenant, ["connection:read", "connection:test"])
    gone, _ = limited_user(owner_db, a.tenant, ["connection:read", "connection:test"], label="gone")
    h = as_user(client, email)
    job = _post(client, h, A, cid)
    hg = as_user(client, gone)
    assert _post(client, hg, A, cid)["id"] == job["id"]  # still the first requester's job
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
    out = job_status(client, h, A, cid, job["id"])
    assert out == {**out, "status": "failed", "result": None, "error": "forbidden"}
    assert [(r[4], r[7]) for r in _jobs(owner_db, cid)] == [("dead", "forbidden")]
    assert _tests(owner_db, cid) == [] and len(rest_server.requests) == calls
    # a requester no longer active in the client: `authz_denied`
    hg = as_user(client, gone)
    second = _post(client, hg, A, cid)
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.users SET status = 'disabled' WHERE primary_email = :e"),
            {"e": gone},
        )
        o.commit()
    assert _execute(client, a.tenant, second["id"])
    owner = as_user(client, people.owner_a)
    out = job_status(client, owner, A, cid, second["id"])
    assert out["status"] == "failed" and out["error"] == "authz_denied" and out["result"] is None
    assert _tests(owner_db, cid) == [] and len(rest_server.requests) == calls


def test_two_posts_share_one_job_and_one_test(
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
    assert len(_tests(owner_db, cid)) == 1
    third = _post(client, h, A, cid)  # once it ended, a new test is a new job
    assert third["id"] != first["id"] and third["status"] == "queued"


def test_a_planned_adapter_is_refused_synchronously_and_queues_nothing(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    body = {
        "name": "planned",
        "connection_type": "data_platform",
        "adapter_key": "snowflake",
        "config": {"account": "a"},
    }
    cid = str(connection(client, h, body, A)["id"])
    r = client.post(f"{A}/connections/{cid}/test", headers=h)
    assert r.status_code == 422 and r.json()["code"] == "adapter_not_implemented", r.text
    assert _jobs(owner_db, cid) == []


def test_re_running_an_executed_job_records_no_second_test(
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
    out = run_test(client, h, A, cid)
    assert out["status"] == "done"
    calls = len(rest_server.requests)
    app: Any = client.app  # Any: the FastAPI app behind the test client
    again = connections.run_test_job(
        app.state.db, app.state.integration, world.a.tenant, UUID(out["id"])
    )
    assert again is None  # already recorded: success, nothing run
    assert [str(r[0]) for r in _tests(owner_db, cid)] == [out["result"]["id"]]
    assert len(rest_server.requests) == calls


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
    assert job_status(client, h, A, cid, job["id"])["status"] == "running"
    with owner_db.session() as o:
        reaped = o.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
        o.commit()
    assert reaped == []  # no run: nothing to settle or audit
    assert [(r[4], r[7]) for r in _jobs(owner_db, cid)] == [("done", "stale lease")]
    out = job_status(client, h, A, cid, job["id"])
    assert (out["status"], out["error"], out["result"]) == ("failed", "worker_lost", None)
