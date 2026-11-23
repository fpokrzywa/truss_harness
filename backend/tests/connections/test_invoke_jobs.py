"""Connection invoke runs on the worker (issue #190, ARCH M2; DECISIONS B-2998, B-3141).

`POST .../connections/{id}/invoke` checks the request, then queues one `connection.invoke` job
(no run) and its `connection_invocations` row (`queued`) in one transaction and answers 202; the
API process never calls the adapter. The job runs at most once as its requester, re-derived from
the database, with the permissions checked again; it moves the invocation `queued -> running ->
succeeded | failed` and stores the scrubbed result there only. `GET .../invocations/{id}` answers
the requester alone. Jobs are executed here by `conntests.execute_invocation`, the worker's own
job function on a job leased only if it is still queued.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any  # Any: JSON bodies, row values
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.testservers import RestTestServer
from shifde.connections import repository as connections_repo
from shifde.connections import service as connections
from shifde.core import db as dbmod
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.redaction import REDACTED
from shifde.core.settings import Settings
from shifde.observability.logging import configure_logging
from tests._support.conntests import (
    execute_invocation,
    invocation_job,
    invocation_status,
    run_invoke,
)
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

_VARYING = {"instance", "request_id"}
ECHO = {"method": "GET", "path": "/echo", "query": {"q": "1"}}


def _q(owner_db: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # Any: row values
    with owner_db.session() as o:
        return [tuple(r) for r in o.execute(text(sql), params).all()]


def _invocations(owner_db: Database, cid: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT id, tenant_id, job_id, connection_id, requested_by, status, result, error_code, "
        "created_at, updated_at, finished_at FROM shifde.connection_invocations "
        "WHERE connection_id = :c ORDER BY created_at, id",
        c=cid,
    )


def _jobs(owner_db: Database, cid: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT id, tenant_id, kind, run_id, status, payload, dedupe_key, last_error "
        "FROM shifde.jobs WHERE kind = 'connection.invoke' AND payload->>'connection_id' = :c "
        "ORDER BY created_at, id",
        c=cid,
    )


KEPT = {
    "invocation_id",
    "connection_id",
    "request_hash",
    "principal_id",
    "principal_type",
    "api_key_id",
}  # an ended invoke job's payload (issue #224)


def _payload(owner_db: Database, cid: str) -> dict[str, Any]:  # Any: JSON values
    [job] = _jobs(owner_db, cid)
    return dict(job[5])


def _assert_cleared(
    owner_db: Database, cid: str, request_hash: str, *markers: str
) -> dict[str, Any]:  # Any: JSON values
    """The ended job's payload kept the ids and the hash, and none of the request's values."""
    payload = _payload(owner_db, cid)
    assert set(payload) == KEPT and payload["request_hash"] == request_hash
    dumped = json.dumps(payload)
    assert all(m not in dumped for m in markers), payload
    return payload


def _marked(tag: str) -> tuple[dict[str, Any], tuple[str, ...]]:  # Any: the invoke request
    """A request carrying a unique value in its query, its `X-Trace` header and its body."""
    q, t, b = (f"{tag}-{part}-{uuid7().hex}" for part in ("query", "trace", "body"))
    return {**ECHO, "query": {"q": q}, "headers": {"X-Trace": t}, "body": {"note": b}}, (q, t, b)


def _active_rest(
    client: TestClient,
    h: dict[str, str],
    A: str,
    app_db: Database,
    settings: Settings,
    tenant: UUID,
    server: RestTestServer,
    name: str = "rest-api",
) -> str:
    sid = secret(app_db, settings, tenant, REST_TOKEN)
    cid = str(connection(client, h, rest_body(server, sid, name), A)["id"])
    activate(client, h, cid, A)
    return cid


def _post(
    client: TestClient,
    h: dict[str, str],
    A: str,
    cid: str,
    body: Any = ECHO,  # Any: the invoke request
    key: str | None = None,
    status: int = 202,
) -> Any:  # Any: JSON body
    headers = {**h, **({"Idempotency-Key": key} if key is not None else {})}
    r = client.post(f"{A}/connections/{cid}/invoke", json=body, headers=headers)
    assert r.status_code == status, r.text
    return r.json()


def _user_id(owner_db: Database, email: str) -> UUID:
    [(uid,)] = _q(owner_db, "SELECT id FROM shifde.users WHERE primary_email = :e", e=email)
    return UUID(str(uid))


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


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
    cid = _active_rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    calls = len(rest_server.requests)
    inv = _post(client, h, A, cid)
    assert len(rest_server.requests) == calls  # no request reached the server during the POST
    assert inv["connection_id"] == cid and inv["status"] == "queued" and inv["id"]
    assert inv["result"] is None and inv["error"] is None and inv["finished_at"] is None
    [(jid, tenant, kind, run_id, status, payload, dedupe, _)] = _jobs(owner_db, cid)
    assert (tenant, kind, run_id, status) == (
        world.a.tenant,
        connections.INVOKE_JOB_KIND,
        None,
        "queued",
    )
    assert dedupe == f"connection-invoke:{inv['id']}"  # no key: every POST is its own
    [row] = _invocations(owner_db, cid)
    owner_id = _user_id(owner_db, people.owner_a)
    assert (str(row[0]), row[1], row[2], row[4], row[5]) == (
        inv["id"],
        world.a.tenant,
        jid,
        owner_id,
        "queued",
    )
    assert payload["invocation_id"] == inv["id"] and payload["connection_id"] == cid
    assert payload["request"]["path"] == "/echo" and payload["write"] is False
    assert payload["principal_id"] == str(owner_id) and payload["principal_type"] == "user"
    assert invocation_status(client, h, A, cid, inv["id"])["status"] == "queued"
    # the worker runs it: the call happens then, and the stored result is the response
    assert execute_invocation(client, A, inv["id"], tenant_id=world.a.tenant)
    assert len(rest_server.requests) == calls + 1
    out = invocation_status(client, h, A, cid, inv["id"])
    assert out["status"] == "succeeded" and out["error"] is None and out["finished_at"]
    assert out["result"]["result"]["status"] == 200
    assert out["result"]["result"]["body"]["query"] == {"q": "1"}
    assert set(out["result"]) == {"result", "truncated", "duration_ms"}
    [row] = _invocations(owner_db, cid)
    assert row[5] == "succeeded" and row[10] is not None and row[4] == owner_id
    assert row[6] == out["result"]
    assert [r[4] for r in _jobs(owner_db, cid)] == ["done"]


@pytest.mark.parametrize(
    "header",
    ["X-Api-Key", "Proxy-Authorization", "authorization", "Cookie", "X-Auth-Token",
     "X-Session-Id", "X-Amz-Signature", "X-Client-Credential", "Host"],
)  # fmt: skip
def test_a_credential_header_is_refused_and_queues_nothing(
    header: str,
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Lead decision B-2999 (1): a request header whose name may carry a credential (a forbidden
    header, or a name holding auth, token, key, secret, password, passwd, cookie, session,
    signature, credential or bearer, in any case) is refused with 422 before anything is queued:
    no job, no invocation, no upstream call."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    calls = len(rest_server.requests)
    body = {**ECHO, "headers": {header: f"caller-secret-{uuid7().hex}", "X-Trace": "t-1"}}
    refused = _post(client, h, A, cid, body, status=422)
    assert refused["code"] == "validation_error"
    assert refused["detail"] == connections.CREDENTIAL_HEADER_REFUSED
    assert refused["detail"] == "credentials come from the connection, not the request"
    assert _jobs(owner_db, cid) == [] and _invocations(owner_db, cid) == []
    assert len(rest_server.requests) == calls


def test_the_payload_holds_no_credential(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Lead decisions B-2998 (b), B-2999 (5): the parsed request is queued in `jobs.payload`
    with no credential (the connection's secret is resolved only by the worker), and the
    enqueue is audited `connection.invoke_queued` with ids only."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, world.a.tenant, rest_server)
    body = {**ECHO, "headers": {"X-Trace": "t-1"}}
    inv = _post(client, h, A, cid, body)
    [(job_id, _, _, _, _, payload, _, _)] = _jobs(owner_db, cid)
    assert set(payload) == {
        "invocation_id",
        "connection_id",
        "request",
        "write",
        "request_hash",
        "principal_id",
        "principal_type",
        "api_key_id",
    }
    assert payload["request"]["headers"] == {"X-Trace": "t-1"}
    assert REST_TOKEN not in json.dumps(payload)
    queued = _q(
        owner_db,
        "SELECT target_id, outcome, details FROM shifde.audit_events "
        "WHERE action = 'connection.invoke_queued' AND target_id = :c",
        c=cid,
    )
    assert [(str(t), o) for t, o, _ in queued] == [(cid, "success")]
    details = {k: v for k, v in queued[0][2].items() if k not in ("actor", "target")}
    assert details == {"invocation_id": inv["id"], "job_id": str(job_id)}
    # the worker still authenticates with the connection's own secret
    assert execute_invocation(client, A, inv["id"], tenant_id=world.a.tenant)
    assert invocation_status(client, h, A, cid, inv["id"])["status"] == "succeeded"
    # a replay queues nothing, so it audits nothing
    _post(client, h, A, cid, body, key="k-1")
    _post(client, h, A, cid, body, key="k-1", status=200)
    assert len(_q(owner_db, "SELECT 1 FROM shifde.audit_events WHERE action = "
                  "'connection.invoke_queued' AND target_id = :c", c=cid)) == 2  # fmt: skip


def test_an_idempotency_key_replays_the_first_invocation(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    first = _post(client, h, A, cid, key="key-1")
    again = _post(client, h, A, cid, key="key-1", status=200)
    assert again["id"] == first["id"] and again["status"] == "queued"
    assert len(_jobs(owner_db, cid)) == 1 and len(_invocations(owner_db, cid)) == 1
    assert execute_invocation(client, A, first["id"], tenant_id=a.tenant)
    calls = len(rest_server.requests)
    done = _post(client, h, A, cid, key="key-1", status=200)  # the first job has ended
    assert done["id"] == first["id"] and done["status"] == "succeeded" and done["result"]
    assert len(_jobs(owner_db, cid)) == 1 and len(rest_server.requests) == calls
    other = _post(client, h, A, cid, {**ECHO, "query": {"q": "2"}}, key="key-1", status=409)
    assert other["code"] == "idempotency_conflict"
    for bad in ("", "x" * 201, "has space"):
        r = client.post(
            f"{A}/connections/{cid}/invoke", json=ECHO, headers={**h, "Idempotency-Key": bad}
        )
        assert r.status_code == 422, (bad, r.text)
    # another key, or no key, is a new invocation; so is the same key of another user
    assert _post(client, h, A, cid, key="key-2")["id"] != first["id"]
    assert _post(client, h, A, cid)["id"] != first["id"]
    email, _ = limited_user(owner_db, a.tenant, ["connection:read", "connection:use"])
    hm = as_user(client, email)
    assert _post(client, hm, A, cid, key="key-1")["id"] != first["id"]
    assert len(_jobs(owner_db, cid)) == 4 and len(_invocations(owner_db, cid)) == 4


def test_two_concurrent_first_requests_share_one_job(
    world: World,
    owner_db: Database,
    app_url: str,
    people: People,
    client: TestClient,
    app_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """`uq_jobs__dedupe` settles a race: the second insert waits for the first transaction and,
    once it commits, inserts nothing and answers the first job."""
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = UUID(_active_rest(client, h, A, app_db, settings, a.tenant, rest_server))
    requester = _user_id(owner_db, people.owner_a)
    key = f"connection-invoke:{cid}:{requester}:race"
    database = Database(app_url, pool_size=2, max_overflow=0)
    out: list[tuple[Any, bool]] = []

    def enqueue(session: Any) -> tuple[Any, bool]:  # Any: a session, a job row
        return connections_repo.enqueue_invoke_job(
            session,
            tenant_id=a.tenant,
            kind=connections.INVOKE_JOB_KIND,
            job_id=uuid7(),
            dedupe_key=key,
            payload={"connection_id": str(cid), "request_hash": "h"},
            invocation_id=uuid7(),
            connection_id=cid,
            requested_by=requester,
        )

    def second() -> None:
        with database.session() as s:
            dbmod.set_tenant(s, a.tenant)
            out.append(enqueue(s))
            s.commit()

    try:
        with database.session() as s1:
            dbmod.set_tenant(s1, a.tenant)
            job, created = enqueue(s1)
            assert created
            racer = threading.Thread(target=second)
            racer.start()
            racer.join(1.0)
            assert racer.is_alive()  # waiting on the first transaction's unique key
            s1.commit()
        racer.join(10.0)
    finally:
        database.dispose()
    [(raced, raced_created)] = out
    assert raced["id"] == job["id"] and raced_created is False
    assert len(_jobs(owner_db, str(cid))) == 1 and len(_invocations(owner_db, str(cid))) == 1


def test_another_member_cannot_read_the_invocation(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Only the requester reads an invocation: another member of the same client, who may read
    and use the connection, gets the unknown-invocation 404; so does a different connection."""
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    other_cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server, "other")
    inv = run_invoke(client, h, A, cid, ECHO, tenant_id=a.tenant)
    assert inv["status"] == "succeeded"
    before = _invocations(owner_db, cid)
    email, _ = limited_user(owner_db, a.tenant, ["connection:read", "connection:use"])
    hm = as_user(client, email)
    url = f"{A}/connections/{cid}/invocations"
    missing = client.get(f"{url}/{uuid7()}", headers=hm)
    assert missing.json()["detail"] == "invocation not found"
    _same_404(client.get(f"{url}/{inv['id']}", headers=hm), missing)
    h = as_user(client, people.owner_a)
    _same_404(
        client.get(f"{A}/connections/{other_cid}/invocations/{inv['id']}", headers=h),
        client.get(f"{A}/connections/{other_cid}/invocations/{uuid7()}", headers=h),
    )
    assert client.get(f"{url}/{inv['id']}", headers=h).status_code == 200
    assert _invocations(owner_db, cid) == before


def test_a_use_only_requester_reads_its_own_invocation(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Lead decision B-2999 (3): a requester holding `connection:use` but not `connection:read`
    reads its own invocation on the status route; another use-only member, and a member holding
    neither, get the unknown-invocation 404 (the requester filter is unchanged)."""
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    email, _ = limited_user(owner_db, a.tenant, ["connection:use"])
    hu = as_user(client, email)
    inv = run_invoke(client, hu, A, cid, ECHO, tenant_id=a.tenant)
    url = f"{A}/connections/{cid}/invocations"
    own = client.get(f"{url}/{inv['id']}", headers=hu)
    assert own.status_code == 200, own.text
    assert own.json()["status"] == "succeeded" and own.json()["result"]["result"]["status"] == 200
    other, _ = limited_user(owner_db, a.tenant, ["connection:use"])
    ho = as_user(client, other)
    _same_404(client.get(f"{url}/{inv['id']}", headers=ho),
              client.get(f"{url}/{uuid7()}", headers=ho))  # fmt: skip
    neither, _ = limited_user(owner_db, a.tenant, ["workspace:read"])
    hn = as_user(client, neither)
    assert client.get(f"{url}/{inv['id']}", headers=hn).status_code == 404


def test_a_finished_job_never_runs_twice(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    inv = run_invoke(client, h, A, cid, ECHO, tenant_id=a.tenant)
    assert inv["status"] == "succeeded"
    before, calls = _invocations(owner_db, cid), len(rest_server.requests)
    app: Any = client.app  # Any: the FastAPI app behind the test client
    job_id = invocation_job(app.state.db, a.tenant, UUID(inv["id"]))
    again = connections.run_invoke_job(app.state.db, app.state.integration, a.tenant, job_id)
    assert again is None
    assert _invocations(owner_db, cid) == before  # same row, same `updated_at`
    assert len(rest_server.requests) == calls
    # the ended job no longer holds the request (issue #224)
    [job] = _jobs(owner_db, cid)
    assert "request" not in job[5] and "write" not in job[5]
    # redelivered (the job queued again): still no second call
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.jobs SET status = 'queued', finished_at = NULL WHERE id = :j"),
            {"j": job_id},
        )
        o.commit()
    assert execute_invocation(client, A, inv["id"], tenant_id=a.tenant)
    assert _invocations(owner_db, cid) == before and len(rest_server.requests) == calls
    # a job without its invocation, or no job at all: not found, nothing written
    assert (
        connections.run_invoke_job(app.state.db, app.state.integration, a.tenant, uuid7())
        == "not_found"
    )


def test_a_running_invocation_is_worker_lost_without_a_call(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """An earlier attempt died mid-call (the row is `running`): the redelivered job marks it
    `failed`/`worker_lost` and never calls the adapter again."""
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    inv = _post(client, h, A, cid)
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.connection_invocations SET status = 'running' WHERE id = :i"),
            {"i": inv["id"]},
        )
        o.commit()
    assert invocation_status(client, h, A, cid, inv["id"])["status"] == "running"
    calls = len(rest_server.requests)
    assert execute_invocation(client, A, inv["id"], tenant_id=a.tenant)
    assert len(rest_server.requests) == calls
    out = invocation_status(client, h, A, cid, inv["id"])
    assert (out["status"], out["error"], out["result"]) == ("failed", "worker_lost", None)
    assert out["finished_at"] is not None
    [row] = _invocations(owner_db, cid)
    assert (row[5], row[7]) == ("failed", "worker_lost")
    assert [r[4] for r in _jobs(owner_db, cid)] == ["done"]


def test_a_queued_invocation_whose_job_ended_reads_as_worker_lost(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """The reaper (or a dead job) closed the job before its invocation ended: the status route
    reports `failed`/`worker_lost` and writes nothing."""
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    inv = _post(client, h, A, cid)
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.jobs SET status = 'dead', last_error = 'stale lease', "
                "finished_at = now() WHERE payload->>'invocation_id' = :i"
            ),
            {"i": inv["id"]},
        )
        o.commit()
    before = _invocations(owner_db, cid)
    out = invocation_status(client, h, A, cid, inv["id"])
    assert (out["status"], out["error"], out["result"]) == ("failed", "worker_lost", None)
    assert _invocations(owner_db, cid) == before and before[0][5] == "queued"


def test_the_stored_result_is_scrubbed(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    leaked = f"leaked-{uuid7().hex}"
    query = {"api_token": leaked, "note": f"Bearer {leaked}", "q": "plain"}
    out = run_invoke(client, h, A, cid, {**ECHO, "query": query}, tenant_id=a.tenant)
    assert out["status"] == "succeeded", out
    echoed = out["result"]["result"]["body"]["query"]
    assert echoed["api_token"] == REDACTED and echoed["q"] == "plain"
    assert leaked not in echoed["note"]
    [row] = _invocations(owner_db, cid)
    assert leaked not in json.dumps(row[6]) and row[6] == out["result"]


def test_no_result_value_reaches_audit_details_or_logs(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whatever ran before: the product's logging setup is applied, the root logger is at INFO
    and the capture sees every record (B-3901: httpx would log the URL with its query)."""
    configure_logging(settings.log_level, settings.log_format)
    logging.getLogger().addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    marker = f"marker-{uuid7().hex}"
    out = run_invoke(client, h, A, cid, {**ECHO, "query": {"q": marker}}, tenant_id=a.tenant)
    assert out["status"] == "succeeded"
    assert out["result"]["result"]["body"]["query"] == {"q": marker}  # the server returned it
    details = _q(
        owner_db,
        "SELECT details, outcome FROM shifde.audit_events WHERE action = 'connection.invoked' "
        "AND target_id = :c",
        c=cid,
    )
    assert [(d["invocation_id"], d["query_keys"], o) for d, o in details] == [
        (out["id"], ["q"], "success")
    ]
    assert all(marker not in json.dumps(d) for d, _ in details)
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err + caplog.text


def test_a_permission_removed_before_the_job_runs_fails_it_without_a_call(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Jobs carry no permissions: `connection:use` is checked again under the re-derived
    requester; the refusal is recorded on the invocation and the job ends `done`."""
    a, A = world.a, org(world.a.slug)
    owner = as_user(client, people.owner_a)
    cid = _active_rest(client, owner, A, app_db, settings, a.tenant, rest_server)
    email, uid = limited_user(owner_db, a.tenant, ["connection:read", "connection:use"])
    h = as_user(client, email)
    inv = _post(client, h, A, cid)
    with owner_db.session() as o:
        o.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
        o.execute(
            text(
                "DELETE FROM shifde.role_permissions WHERE permission_key = 'connection:use' "
                "AND role_id IN (SELECT role_id FROM shifde.role_bindings WHERE subject_id = :u)"
            ),
            {"u": uid},
        )
        o.commit()
    calls = len(rest_server.requests)
    assert execute_invocation(client, A, inv["id"], tenant_id=a.tenant)
    assert len(rest_server.requests) == calls
    out = invocation_status(client, h, A, cid, inv["id"])
    assert (out["status"], out["error"], out["result"]) == ("failed", "forbidden", None)
    assert [(r[4], r[7]) for r in _jobs(owner_db, cid)] == [("done", None)]
    [row] = _invocations(owner_db, cid)
    assert (row[5], row[6], row[7]) == ("failed", None, "forbidden")


def test_an_adapter_failure_stores_only_its_code(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    out = run_invoke(
        client, h, A, cid, {"method": "GET", "path": "http://evil.test/x"}, tenant_id=a.tenant
    )
    assert (out["status"], out["error"], out["result"]) == ("failed", "validation_error", None)
    [row] = _invocations(owner_db, cid)
    assert (row[5], row[6], row[7]) == ("failed", None, "validation_error")
    [(details, outcome)] = _q(
        owner_db,
        "SELECT details, outcome FROM shifde.audit_events WHERE action = 'connection.invoked' "
        "AND target_id = :c",
        c=cid,
    )
    assert outcome == "failure" and details["error_class"] == "config"
    assert details["invocation_id"] == out["id"]


def test_a_refusal_before_the_queue_queues_nothing(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    bad = _post(client, h, A, cid, {"path": 7}, status=422)
    assert bad["code"] == "validation_error"
    write = _post(client, h, A, cid, {**ECHO, "write": True}, status=403)  # read-only
    assert write["code"] == "forbidden"
    assert _jobs(owner_db, cid) == [] and _invocations(owner_db, cid) == []


def test_a_succeeded_invoke_job_keeps_no_request_and_replays_the_same_invocation(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Issue #224: the statement that ends the job drops `request` and `write` from its payload;
    the ids and `request_hash` stay, so a replay with the same Idempotency-Key still answers the
    first invocation and a different body is still a conflict."""
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    body, markers = _marked("ok")
    first = _post(client, h, A, cid, body, key="clear-1")
    queued = _payload(owner_db, cid)
    assert queued["request"]["headers"] == {"X-Trace": markers[1]}
    assert all(m in json.dumps(queued) for m in markers)
    assert execute_invocation(client, A, first["id"], tenant_id=a.tenant)
    out = invocation_status(client, h, A, cid, first["id"])
    assert out["status"] == "succeeded", out
    _assert_cleared(owner_db, cid, queued["request_hash"], *markers)
    assert [r[4] for r in _jobs(owner_db, cid)] == ["done"]
    # a replay of the ended, cleared invocation: the same one, nothing new, no second call
    jobs, invocations, calls = (
        _jobs(owner_db, cid),
        _invocations(owner_db, cid),
        len(rest_server.requests),
    )
    again = _post(client, h, A, cid, body, key="clear-1", status=200)
    assert (again["id"], again["status"]) == (first["id"], "succeeded")
    assert again["result"] == out["result"]
    assert _jobs(owner_db, cid) == jobs and _invocations(owner_db, cid) == invocations
    assert len(rest_server.requests) == calls
    other = _post(client, h, A, cid, {**body, "query": {"q": "2"}}, key="clear-1", status=409)
    assert other["code"] == "idempotency_conflict"
    assert _jobs(owner_db, cid) == jobs and _invocations(owner_db, cid) == invocations


def test_a_failed_invoke_job_keeps_no_request(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Issue #224: an adapter failure ends the invocation `failed` and its job `done` without
    the request, as a success does."""
    a, A, h = world.a, org(world.a.slug), as_user(client, people.owner_a)
    cid = _active_rest(client, h, A, app_db, settings, a.tenant, rest_server)
    body, markers = _marked("bad")
    inv = _post(client, h, A, cid, {**body, "path": "http://evil.test/x"})
    request_hash = _payload(owner_db, cid)["request_hash"]
    assert execute_invocation(client, A, inv["id"], tenant_id=a.tenant)
    out = invocation_status(client, h, A, cid, inv["id"])
    assert (out["status"], out["error"]) == ("failed", "validation_error")
    _assert_cleared(owner_db, cid, request_hash, *markers)
    assert [r[4] for r in _jobs(owner_db, cid)] == ["done"]


def test_a_refused_invoke_job_keeps_no_request(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    rest_server: RestTestServer,
) -> None:
    """Issue #224: `connection:use` removed before the job runs fails the invocation
    `forbidden`; the ended job holds no request either."""
    a, A = world.a, org(world.a.slug)
    owner = as_user(client, people.owner_a)
    cid = _active_rest(client, owner, A, app_db, settings, a.tenant, rest_server)
    email, uid = limited_user(owner_db, a.tenant, ["connection:read", "connection:use"])
    h = as_user(client, email)
    body, markers = _marked("refused")
    inv = _post(client, h, A, cid, body)
    request_hash = _payload(owner_db, cid)["request_hash"]
    with owner_db.session() as o:
        o.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
        o.execute(
            text(
                "DELETE FROM shifde.role_permissions WHERE permission_key = 'connection:use' "
                "AND role_id IN (SELECT role_id FROM shifde.role_bindings WHERE subject_id = :u)"
            ),
            {"u": uid},
        )
        o.commit()
    calls = len(rest_server.requests)
    assert execute_invocation(client, A, inv["id"], tenant_id=a.tenant)
    assert len(rest_server.requests) == calls
    out = invocation_status(client, h, A, cid, inv["id"])
    assert (out["status"], out["error"]) == ("failed", "forbidden")
    _assert_cleared(owner_db, cid, request_hash, *markers)
