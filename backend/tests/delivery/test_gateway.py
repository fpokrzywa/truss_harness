"""Executor gateway job queue (design sections 4 and 5, section 9 items 5, 7, 8 and 9; B-2669):
lease and replay, `SKIP LOCKED` concurrency, heartbeat and the lease proof, credential grant,
artifact, result, the lost-lease sweep (an `apply` job is never retried) and what `shifde_app`
may not do to the queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any  # Any: JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import Conflict
from shifde.core.ids import uuid7
from shifde.delivery import gateway
from tests.connections.routes import as_user
from tests.delivery.conftest import ExecutorSide, JobKinds, ProbeResult, make_executor
from tests.delivery.routes import (
    address,
    enqueue,
    expire,
    job_row,
    job_url,
    lease,
    owner_sql,
    proof,
    register,
    target_body,
    targets,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws_slug

DIGEST = "a" * 64
LEASE_SQL = text("SELECT * FROM shifde.delivery_lease_job(:e, :r, CAST(:k AS text[]))")


def _db_now(owner_db: Database) -> Any:  # Any: a datetime
    with owner_db.session() as o:
        return o.execute(text("SELECT now()")).scalar_one()


def _leased(r: httpx.Response) -> dict[str, Any]:
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _post(client: TestClient, ex: ExecutorSide, job: object, action: str, body: Any) -> Any:
    return client.post(
        job_url(job, action), json=body, headers={"Authorization": f"Bearer {ex.token}"}
    )


def _code(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status and r.json()["code"] == code, r.text


# ------------------------------------------------------------------ lease and replay
def test_lease_answers_204_without_a_due_job(
    client: TestClient, executor_a: ExecutorSide, app_db: Database
) -> None:
    r = lease(client, executor_a.token)
    assert r.status_code == 204 and r.content == b""
    enqueue(app_db, executor_a.tenant, "observe", run_after=utcnow() + timedelta(hours=1))
    assert lease(client, executor_a.token).status_code == 204


def test_lease_returns_the_job_with_a_45_second_lease(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    deployment = uuid7()
    job = enqueue(app_db, executor_a.tenant, "observe", deployment_id=deployment)
    request_id = uuid7()
    body = _leased(lease(client, executor_a.token, request_id=request_id))
    assert set(body) == {"job", "lease_id", "fence", "lease_until", "request_id"}
    assert body["job"] == {
        "id": str(job),
        "kind": "observe",
        "plan_id": None,
        "deployment_id": str(deployment),
        "target_id": None,
        "deadline": None,
        "attempts": 1,
        "max_attempts": gateway.READ_ONLY_MAX_ATTEMPTS,
    }
    assert body["fence"] == 1 and body["request_id"] == str(request_id)
    row = job_row(owner_db, job)
    assert row["status"] == "leased" and str(row["lease_id"]) == body["lease_id"]
    assert row["executor_id"] == UUID(executor_a.executor_id)
    assert abs((row["lease_until"] - _db_now(owner_db)) - timedelta(seconds=45)) < timedelta(
        seconds=5
    )


def test_a_replayed_request_id_returns_the_same_lease_until_the_job_is_done(
    client: TestClient,
    executor_a: ExecutorSide,
    app_db: Database,
    owner_db: Database,
    job_kinds: JobKinds,
) -> None:
    enqueue(app_db, executor_a.tenant, "observe")
    second = enqueue(app_db, executor_a.tenant, "observe")
    request_id = uuid7()
    first = _leased(lease(client, executor_a.token, request_id=request_id))
    again = _leased(lease(client, executor_a.token, request_id=request_id))
    assert again == first
    assert job_row(owner_db, second)["status"] == "queued"  # the replay leased nothing new
    done = _post(
        client,
        executor_a,
        first["job"]["id"],
        "result",
        {**proof(first), "status": "succeeded", "value": {"observed": 1}},
    )
    assert done.status_code == 200, done.text
    assert lease(client, executor_a.token, request_id=request_id).status_code == 204
    assert job_row(owner_db, second)["status"] == "queued"


def test_the_kinds_filter(client: TestClient, executor_a: ExecutorSide, app_db: Database) -> None:
    job = enqueue(app_db, executor_a.tenant, "verify")
    assert lease(client, executor_a.token, kinds=["observe", "apply"]).status_code == 204
    assert _leased(lease(client, executor_a.token, kinds=["verify"]))["job"]["id"] == str(job)
    for kinds in ([], ["observe", "observe"], ["deploy"]):
        r = lease(client, executor_a.token, kinds=kinds)
        assert r.status_code == 422, (kinds, r.text)


def test_two_concurrent_leases_never_take_the_same_job(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, app_url: str
) -> None:
    jobs = {enqueue(app_db, executor_a.tenant, "observe") for _ in range(2)}
    params = {"e": executor_a.executor_id, "k": ["observe"]}
    two_connections = Database(app_url, pool_size=2, max_overflow=0)
    with (
        app_session(two_connections, executor_a.tenant) as s1,
        app_session(two_connections, executor_a.tenant) as s2,
    ):
        one = s1.execute(LEASE_SQL, {**params, "r": uuid7()}).mappings().one()
        # s1 still holds its row lock: s2 skips it instead of waiting or taking it
        two = s2.execute(LEASE_SQL, {**params, "r": uuid7()}).mappings().one()
        three = s2.execute(LEASE_SQL, {**params, "r": uuid7()}).mappings().first()
        s1.commit()
        s2.commit()
    two_connections.dispose()
    assert {one["job_id"], two["job_id"]} == jobs
    assert three is None


# ------------------------------------------------------------------ heartbeat
def test_heartbeat_extends_the_lease_and_refuses_a_stale_proof(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    job = enqueue(app_db, executor_a.tenant, "observe")
    leased = _leased(lease(client, executor_a.token))
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_jobs SET lease_until = now() + interval '5 seconds' WHERE id = :j",
        j=job,
    )
    r = _post(client, executor_a, job, "heartbeat", proof(leased))
    assert r.status_code == 200 and set(r.json()) == {"lease_until"}, r.text
    row = job_row(owner_db, job)
    assert abs((row["lease_until"] - _db_now(owner_db)) - timedelta(seconds=45)) < timedelta(
        seconds=5
    )
    for name, body in (
        ("stale lease_id", {"lease_id": str(uuid7()), "fence": leased["fence"]}),
        ("stale fence", {"lease_id": leased["lease_id"], "fence": 2}),
    ):
        r = _post(client, executor_a, job, "heartbeat", body)
        assert r.status_code == 409 and r.json()["code"] == "lease_lost", (name, r.text)
    expire(owner_db, job)
    _code(_post(client, executor_a, job, "heartbeat", proof(leased)), 409, "lease_lost")
    # an expired lease is never revived
    assert job_row(owner_db, job)["lease_until"] < _db_now(owner_db)
    missing = _post(client, executor_a, uuid7(), "heartbeat", proof(leased))
    _code(missing, 404, "not_found")


# ------------------------------------------------------------------ credential grant
def test_credential_grant(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    app_db: Database,
    owner_db: Database,
) -> None:
    h = as_user(client, people.owner_a)
    url = address()
    target = register(client, h, world.a.slug, target_body(ws_slug(world.a), url))
    client.cookies.clear()
    job = enqueue(app_db, executor_a.tenant, "observe", target_id=UUID(target["id"]))
    leased = _leased(lease(client, executor_a.token))
    r = _post(client, executor_a, job, "credential-grant", proof(leased))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "job_id": str(job),
        "executor_id": executor_a.executor_id,
        "target_id": target["id"],
        "credential_ref": "databricks-dev",
        "audience": target["address"],
        "expires_at": body["expires_at"],
    }
    assert executor_a.token not in r.text and executor_a.token.split("_")[2] not in r.text
    row = job_row(owner_db, job)
    expires = datetime.fromisoformat(body["expires_at"])
    assert expires < row["lease_until"]
    assert abs((expires - _db_now(owner_db)) - timedelta(seconds=30)) < timedelta(seconds=5)
    # a lease ending sooner than 30 s bounds the grant
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_jobs SET lease_until = now() + interval '10 seconds' WHERE id = :j",
        j=job,
    )
    short = _post(client, executor_a, job, "credential-grant", proof(leased)).json()
    assert datetime.fromisoformat(short["expires_at"]) == job_row(owner_db, job)["lease_until"]
    # an archived target is unavailable
    h = as_user(client, people.owner_a)
    r = client.post(f"{targets(world.a.slug)}/{target['id']}/archive", headers=h)
    assert r.status_code == 200, r.text
    client.cookies.clear()
    _code(
        _post(client, executor_a, job, "credential-grant", proof(leased)), 409, "target_unavailable"
    )


def test_credential_grant_without_a_target(
    client: TestClient, executor_a: ExecutorSide, app_db: Database
) -> None:
    job = enqueue(app_db, executor_a.tenant, "observe")
    leased = _leased(lease(client, executor_a.token))
    r = _post(client, executor_a, job, "credential-grant", proof(leased))
    _code(r, 409, "target_unavailable")
    _code(
        _post(client, executor_a, job, "credential-grant", {**proof(leased), "fence": 9}),
        409,
        "lease_lost",
    )


# ------------------------------------------------------------------ artifact
def test_artifact_is_not_found_under_a_valid_lease(
    client: TestClient, executor_a: ExecutorSide, app_db: Database
) -> None:
    job = enqueue(app_db, executor_a.tenant, "plan")
    leased = _leased(lease(client, executor_a.token))
    headers = {"Authorization": f"Bearer {executor_a.token}"}
    r = client.get(job_url(job, "artifact"), params=proof(leased), headers=headers)
    _code(r, 404, "artifact_not_found")
    stale = {"lease_id": str(uuid7()), "fence": leased["fence"]}
    _code(client.get(job_url(job, "artifact"), params=stale, headers=headers), 409, "lease_lost")
    r = client.get(job_url(job, "artifact"), params={**proof(leased), "fence": 0}, headers=headers)
    assert r.status_code == 422, r.text


# ------------------------------------------------------------------ result
def test_a_valid_result_finishes_the_job_in_the_hooks_transaction(
    client: TestClient,
    executor_a: ExecutorSide,
    app_db: Database,
    owner_db: Database,
    job_kinds: JobKinds,
) -> None:
    job = enqueue(app_db, executor_a.tenant, "observe")
    leased = _leased(lease(client, executor_a.token))
    bad = _post(
        client, executor_a, job, "result", {**proof(leased), "status": "succeeded", "value": {}}
    )
    _code(bad, 422, "validation_error")
    assert job_row(owner_db, job)["status"] == "leased" and job_kinds.calls == []
    for body in (
        {"status": "succeeded"},
        {"status": "succeeded", "value": {"observed": 1}, "error_code": "execution_failed"},
        {"status": "failed"},
        {"status": "failed", "error_code": "execution_failed", "value": {"observed": 1}},
        {"status": "failed", "error_code": "sandbox_failed"},
    ):
        r = _post(client, executor_a, job, "result", {**proof(leased), **body})
        assert r.status_code == 422, (body, r.text)
    good = _post(
        client,
        executor_a,
        job,
        "result",
        {**proof(leased), "status": "succeeded", "value": {"observed": 3}},
    )
    assert good.status_code == 200 and good.json() == {"job_id": str(job), "status": "done"}
    row = job_row(owner_db, job)
    assert row["status"] == "done"
    assert row["result"] == {"status": "succeeded", "value": {"observed": 3}}
    assert job_kinds.calls == [(job, "done")]  # the hook saw the uncommitted update
    again = _post(
        client,
        executor_a,
        job,
        "result",
        {**proof(leased), "status": "succeeded", "value": {"observed": 3}},
    )
    _code(again, 409, "lease_lost")


def test_a_failed_result(
    client: TestClient,
    executor_a: ExecutorSide,
    app_db: Database,
    owner_db: Database,
    job_kinds: JobKinds,
) -> None:
    job = enqueue(app_db, executor_a.tenant, "apply", deployment_id=uuid7())
    leased = _leased(lease(client, executor_a.token))
    r = _post(
        client,
        executor_a,
        job,
        "result",
        {**proof(leased), "status": "failed", "error_code": "credential_unavailable"},
    )
    assert r.status_code == 200, r.text
    row = job_row(owner_db, job)
    assert row["status"] == "done"
    assert row["result"] == {"status": "failed", "error_code": "credential_unavailable"}


def test_a_result_for_an_unregistered_kind_is_refused(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    assert gateway.JOB_KINDS == {}  # production code registers no kind yet
    job = enqueue(app_db, executor_a.tenant, "observe")
    leased = _leased(lease(client, executor_a.token))
    r = _post(
        client,
        executor_a,
        job,
        "result",
        {**proof(leased), "status": "succeeded", "value": {"observed": 1}},
    )
    _code(r, 409, "job_kind_unsupported")
    assert job_row(owner_db, job)["status"] == "leased"


def test_a_raising_hook_leaves_the_job_leased(
    client: TestClient,
    executor_a: ExecutorSide,
    app_db: Database,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_: object) -> None:
        raise Conflict("hook refused")

    monkeypatch.setitem(
        gateway.JOB_KINDS, "observe", gateway.JobKindDefinition(ProbeResult, refuse)
    )
    job = enqueue(app_db, executor_a.tenant, "observe")
    leased = _leased(lease(client, executor_a.token))
    r = _post(
        client,
        executor_a,
        job,
        "result",
        {**proof(leased), "status": "succeeded", "value": {"observed": 1}},
    )
    _code(r, 409, "conflict")
    row = job_row(owner_db, job)
    assert row["status"] == "leased" and row["result"] is None


# ------------------------------------------------------------------ the lost-lease sweep
def test_an_expired_apply_job_without_an_intent_is_lost_and_failed(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    job = enqueue(app_db, executor_a.tenant, "apply", deployment_id=uuid7())
    assert job_row(owner_db, job)["max_attempts"] == 1
    leased = _leased(lease(client, executor_a.token, kinds=["apply"]))
    assert leased["job"]["max_attempts"] == 1
    expire(owner_db, job)
    assert lease(client, executor_a.token, kinds=["apply"]).status_code == 204
    row = job_row(owner_db, job)
    assert row["status"] == "lost" and row["attempts"] == 1
    assert row["result"] == {"outcome": "failed", "reason": "no platform write was attempted"}
    assert lease(client, executor_a.token).status_code == 204
    assert job_row(owner_db, job)["status"] == "lost"


def test_an_expired_apply_job_with_an_intent_has_an_unknown_outcome(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    job = enqueue(app_db, executor_a.tenant, "apply", deployment_id=uuid7())
    leased = _leased(lease(client, executor_a.token, kinds=["apply"]))
    intent = {
        **proof(leased),
        "sequence": 1,
        "step_key": "deploy_app",
        "state": "intent",
        "input_digest": DIGEST,
    }
    assert _post(client, executor_a, job, "checkpoint", intent).status_code == 200
    expire(owner_db, job)
    assert lease(client, executor_a.token).status_code == 204
    row = job_row(owner_db, job)
    assert row["status"] == "lost" and row["result"] == {"outcome": "unknown_outcome"}
    assert lease(client, executor_a.token).status_code == 204
    # the old lease can do nothing more
    _code(_post(client, executor_a, job, "heartbeat", proof(leased)), 409, "lease_lost")


def test_an_expired_apply_job_with_only_complete_steps_has_an_unknown_outcome(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    """A finished step is a platform write: losing the lease before the next intent is still an
    unknown outcome, never `failed` (isolation review 1 of SP2-4, finding 1)."""
    job = enqueue(app_db, executor_a.tenant, "apply", deployment_id=uuid7())
    leased = _leased(lease(client, executor_a.token, kinds=["apply"]))
    step = {**proof(leased), "step_key": "bundle_deploy", "input_digest": DIGEST}
    r = _post(client, executor_a, job, "checkpoint", {**step, "sequence": 1, "state": "intent"})
    assert r.status_code == 200, r.text
    complete = {**step, "sequence": 2, "state": "complete", "receipt": {"deployed": True}}
    assert _post(client, executor_a, job, "checkpoint", complete).status_code == 200
    expire(owner_db, job)
    assert lease(client, executor_a.token).status_code == 204
    row = job_row(owner_db, job)
    assert row["status"] == "lost" and row["result"] == {"outcome": "unknown_outcome"}


def test_an_expired_observe_job_is_requeued_until_max_attempts(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    job = enqueue(app_db, executor_a.tenant, "observe")
    first = _leased(lease(client, executor_a.token))
    assert first["fence"] == 1
    expire(owner_db, job)
    second = _leased(lease(client, executor_a.token))
    assert second["job"]["id"] == str(job) and second["fence"] == 2
    assert second["job"]["attempts"] == 2 and second["lease_id"] != first["lease_id"]
    _code(_post(client, executor_a, job, "heartbeat", proof(first)), 409, "lease_lost")
    assert _post(client, executor_a, job, "heartbeat", proof(second)).status_code == 200
    for fence in (3, 4):
        expire(owner_db, job)
        assert _leased(lease(client, executor_a.token))["fence"] == fence
    expire(owner_db, job)
    assert lease(client, executor_a.token).status_code == 204
    row = job_row(owner_db, job)
    assert row["status"] == "lost" and row["attempts"] == gateway.READ_ONLY_MAX_ATTEMPTS
    assert row["result"] == {"outcome": "failed", "reason": "executor lost"}


# ------------------------------------------------------------------ what shifde_app may not do
def test_apply_jobs_get_one_attempt(executor_a: ExecutorSide, app_db: Database) -> None:
    with app_session(app_db, executor_a.tenant) as s, pytest.raises(IntegrityError) as exc:
        s.execute(
            text(
                "INSERT INTO shifde.delivery_jobs (tenant_id, id, kind, max_attempts) "
                "VALUES (:t, :i, 'apply', 2)"
            ),
            {"t": executor_a.tenant, "i": uuid7()},
        )
    assert "ck_delivery_jobs__apply_once" in str(exc.value)


def _refused(app_db: Database, tenant: UUID | None, sql: str, **params: Any) -> None:
    with app_session(app_db, tenant) as s, pytest.raises(DBAPIError) as exc:
        s.execute(text(sql), params)
    assert getattr(exc.value.orig, "sqlstate", None) == "42501", sql


def test_shifde_app_cannot_forge_a_lease_or_delete_queue_rows(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    app_db: Database,
    owner_db: Database,
) -> None:
    deployment = uuid7()
    job = enqueue(app_db, executor_a.tenant, "observe", deployment_id=deployment)
    leased = _leased(lease(client, executor_a.token))
    intent = {
        **proof(leased),
        "sequence": 1,
        "step_key": "deploy_app",
        "state": "intent",
        "input_digest": DIGEST,
    }
    assert _post(client, executor_a, job, "checkpoint", intent).status_code == 200
    for column, value in (
        ("lease_id", "gen_random_uuid()"),
        ("fence", "fence + 1"),
        ("executor_id", "executor_id"),
        ("lease_request_id", "gen_random_uuid()"),
        ("attempts", "0"),
    ):
        _refused(
            app_db,
            executor_a.tenant,
            f"UPDATE shifde.delivery_jobs SET {column} = {value} WHERE id = :j",
            j=job,
        )
    _refused(app_db, executor_a.tenant, "DELETE FROM shifde.delivery_jobs WHERE id = :j", j=job)
    _refused(
        app_db, executor_a.tenant, "DELETE FROM shifde.delivery_journal WHERE job_id = :j", j=job
    )
    # another organization sees none of A's rows
    b = make_executor(client, world.b, people.owner_b)
    with app_session(app_db, b.tenant) as s:
        for table in ("delivery_jobs", "delivery_journal"):
            n = s.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :t"),
                {"t": executor_a.tenant},
            ).scalar_one()
            assert n == 0, table
        assert (
            s.execute(LEASE_SQL, {"e": b.executor_id, "r": uuid7(), "k": ["observe"]}).first()
            is None
        )
    # no organization context, or an executor of another organization: refused
    _refused(
        app_db,
        None,
        "SELECT * FROM shifde.delivery_lease_job(:e, :r, ARRAY['observe'])",
        e=executor_a.executor_id,
        r=uuid7(),
    )
    _refused(
        app_db,
        b.tenant,
        "SELECT * FROM shifde.delivery_lease_job(:e, :r, ARRAY['observe'])",
        e=executor_a.executor_id,
        r=uuid7(),
    )
    row = job_row(owner_db, job)
    assert row["status"] == "leased" and row["fence"] == 1


def test_shifde_app_cannot_revive_a_lease(
    client: TestClient, executor_a: ExecutorSide, app_db: Database, owner_db: Database
) -> None:
    """Isolation review 1 of SP2-4, finding 2: a re-queued, lost or expired job keeps its old
    executor, `lease_id` and `fence`; only a new fence (the lease definer) makes a job leased."""
    observe = enqueue(app_db, executor_a.tenant, "observe")
    apply = enqueue(app_db, executor_a.tenant, "apply", deployment_id=uuid7())
    old = {
        job: _leased(lease(client, executor_a.token, kinds=[kind]))
        for job, kind in ((observe, "observe"), (apply, "apply"))
    }
    revive = (
        "UPDATE shifde.delivery_jobs SET status = 'leased', "
        "lease_until = now() + interval '1 hour' WHERE id = :j"
    )
    for job in (observe, apply):
        expire(owner_db, job)
        # expired but not yet swept: the lease cannot be extended
        _refused(
            app_db,
            executor_a.tenant,
            "UPDATE shifde.delivery_jobs SET lease_until = now() + interval '1 hour' WHERE id = :j",
            j=job,
        )
    assert lease(client, executor_a.token, kinds=["verify"]).status_code == 204  # the sweep
    assert job_row(owner_db, observe)["status"] == "queued"
    assert job_row(owner_db, apply)["status"] == "lost"
    for job in (observe, apply):
        _refused(app_db, executor_a.tenant, revive, j=job)
        row = job_row(owner_db, job)
        assert row["status"] in ("queued", "lost") and row["fence"] == 1
        _code(_post(client, executor_a, job, "heartbeat", proof(old[job])), 409, "lease_lost")
    # the definer still leases the re-queued job, under a new fence
    assert _leased(lease(client, executor_a.token, kinds=["observe"]))["fence"] == 2
