"""Provider health checks and model discoveries run on the worker (ARCH M2, issue #47, B-2720).

`POST .../model-providers/{id}/health-check` and `.../discover-models` check permission, the
provider and its adapter, queue one `model.health_check` / `model.discover` job (no run) in the
caller's client and answer 202; the API process never calls the adapter. The job runs as its
requester, re-derived from the database, with `model:manage` checked again; the status routes
report `queued | running | done | failed` and the result, which the worker stores in the call's
audit event. Jobs are executed here by `conntests.execute_job` with the job's kind, the worker's
own job function on a job leased only if it is still queued. The rotated-key health check is in
`test_p3_models.py::test_unhealthy_or_disabled_providers_block_invocation`.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any  # Any: JSON bodies, row values
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shifde.adapters.base import AdapterFailure, ErrorClass, HealthResult
from shifde.adapters.model_providers import FakeProvider
from shifde.audit import service as audit_service
from shifde.core.db import Database
from shifde.models_admin import repository
from shifde.models_admin import service as models_admin
from tests._support.modeljobs import execute, job_status, post_job, run_discovery, run_job
from tests.connections.routes import as_user, connection, grant, limited_user, org
from tests.models_admin.conftest import provider_values
from tests.models_admin.routes import fake_model_stack
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

HEALTH, DISCOVER = models_admin.HEALTH_JOB_KIND, models_admin.DISCOVER_JOB_KIND


def _q(owner_db: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # Any: row values
    with owner_db.session() as o:
        return [tuple(r) for r in o.execute(text(sql), params).all()]


def _jobs(owner_db: Database, pid: str, kind: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT id, kind, run_id, status, dedupe_key, last_error FROM shifde.jobs "
        "WHERE kind = :k AND payload->>'provider_id' = :p ORDER BY created_at, id",
        k=kind,
        p=pid,
    )


def _events(owner_db: Database, pid: str, action: str) -> list[tuple[Any, ...]]:
    return _q(
        owner_db,
        "SELECT correlation_id, details FROM shifde.audit_events WHERE action = :a "
        "AND target_id = :p ORDER BY occurred_at",
        a=action,
        p=pid,
    )


def _checked_at(owner_db: Database, pid: str) -> Any:
    return _q(
        owner_db,
        "SELECT health_status, health_checked_at FROM shifde.model_providers WHERE id = :p",
        p=pid,
    )


@pytest.fixture
def adapter_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every health / list_models call of the fake provider, still answered as before."""
    calls: list[str] = []
    health, list_models = FakeProvider.health, FakeProvider.list_models

    def on_health(self: Any, ctx: Any, conn: Any) -> Any:  # Any: adapter call arguments
        calls.append("health")
        return health(self, ctx, conn)

    def on_list(self: Any, ctx: Any, conn: Any) -> Any:  # Any: adapter call arguments
        calls.append("list_models")
        return list_models(self, ctx, conn)

    monkeypatch.setattr(FakeProvider, "health", on_health)
    monkeypatch.setattr(FakeProvider, "list_models", on_list)
    return calls


def test_a_health_check_post_queues_a_job_and_never_calls_the_adapter(
    client: TestClient, world: World, people: People, owner_db: Database, adapter_calls: list[str]
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    adapter_calls.clear()  # activating the connection ran its test
    before = _checked_at(owner_db, pid)
    job = post_job(client, h, A, pid, HEALTH)
    assert job["result"] is None and job["error"] is None and job["provider_id"] == pid
    assert [r[1:5] for r in _jobs(owner_db, pid, HEALTH)] == [
        (HEALTH, None, "queued", f"model-health-check:{pid}")
    ]
    assert adapter_calls == [] and _checked_at(owner_db, pid) == before  # not run yet
    assert job_status(client, h, A, pid, HEALTH, job["id"])["status"] == "queued"
    assert execute(client, A, HEALTH, job["id"])
    out = job_status(client, h, A, pid, HEALTH, job["id"])
    assert out["status"] == "done" and out["error"] is None and adapter_calls[0] == "health"
    assert set(out["result"]) == {"status", "latency_ms", "checked_at", "detail"}
    assert out["result"]["status"] == "healthy" and out["result"]["latency_ms"] >= 0
    provider = client.get(f"{A}/model-providers/{pid}", headers=h).json()
    assert provider["health_status"] == "healthy"
    [(correlation, details)] = _events(owner_db, pid, "model.provider.health_checked")
    assert correlation == f"model-health-check-{job['id']}" and details["status"] == "healthy"


def test_a_discovery_post_queues_a_job_and_returns_the_full_model_list(
    client: TestClient, world: World, people: People, owner_db: Database, adapter_calls: list[str]
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    adapter_calls.clear()  # activating the connection ran its test
    job = post_job(client, h, A, pid, DISCOVER)
    assert job["result"] is None
    assert [r[1:5] for r in _jobs(owner_db, pid, DISCOVER)] == [
        (DISCOVER, None, "queued", f"model-discover:{pid}")
    ]
    assert adapter_calls == []
    assert execute(client, A, DISCOVER, job["id"])
    out = job_status(client, h, A, pid, DISCOVER, job["id"])
    assert out["status"] == "done" and adapter_calls == ["list_models"]
    assert out["result"] == [
        {
            "model_key": "fake-chat",
            "display_name": "fake-chat",
            "context_window": 8192,
            "max_output_tokens": 2048,
            "capabilities": ["chat"],
        }
    ]
    [(correlation, details)] = _events(owner_db, pid, "model.provider.models_discovered")
    assert correlation == f"model-discover-{job['id']}" and details["count"] == 1
    assert details["models"][0]["max_output"] == 2048  # stored under a key `scrub` keeps


def test_a_failing_health_check_is_recorded_unavailable_and_ends_done(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]

    def fail(self: Any, ctx: Any, conn: Any) -> Any:  # Any: adapter call arguments
        raise AdapterFailure(ErrorClass.AUTH, "the key was rejected")

    monkeypatch.setattr(FakeProvider, "health", fail)
    out = run_job(client, h, A, pid, HEALTH)
    assert out["status"] == "done" and out["result"]["status"] == "unavailable"
    assert out["result"]["detail"] == "the key was rejected"
    assert [r[3] for r in _jobs(owner_db, pid, HEALTH)] == ["done"]
    assert _checked_at(owner_db, pid)[0][0] == "unavailable"


def test_a_failing_discovery_fails_the_job_and_records_nothing(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]

    def fail(self: Any, ctx: Any, conn: Any) -> Any:  # Any: adapter call arguments
        raise AdapterFailure(ErrorClass.UPSTREAM, "the provider is down")

    monkeypatch.setattr(FakeProvider, "list_models", fail)
    out = run_job(client, h, A, pid, DISCOVER)
    assert (out["status"], out["result"], out["error"]) == ("failed", None, "upstream_error")
    assert [(r[3], r[5]) for r in _jobs(owner_db, pid, DISCOVER)] == [("dead", "upstream_error")]
    assert _events(owner_db, pid, "model.provider.models_discovered") == []


def test_a_permission_removed_before_the_job_runs_fails_it_and_records_nothing(
    client: TestClient, world: World, people: People, owner_db: Database, adapter_calls: list[str]
) -> None:
    a, A = world.a, org(world.a.slug)
    pid = fake_model_stack(client, as_user(client, people.owner_a), A)["provider"]
    adapter_calls.clear()  # activating the connection ran its test
    email, uid = limited_user(owner_db, a.tenant, ["model:read", "model:manage"])
    h = as_user(client, email)
    jobs = {kind: post_job(client, h, A, pid, kind) for kind in (HEALTH, DISCOVER)}
    with owner_db.session() as o:
        o.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
        o.execute(
            text(
                "DELETE FROM shifde.role_permissions WHERE permission_key = 'model:manage' "
                "AND role_id IN (SELECT role_id FROM shifde.role_bindings WHERE subject_id = :u)"
            ),
            {"u": uid},
        )
        o.commit()
    before = _checked_at(owner_db, pid)
    for kind, job in jobs.items():
        assert execute(client, A, kind, job["id"])
        out = job_status(client, h, A, pid, kind, job["id"])  # `model:read` still reads it
        assert out == {**out, "status": "failed", "result": None, "error": "forbidden"}
        assert [(r[3], r[5]) for r in _jobs(owner_db, pid, kind)] == [("dead", "forbidden")]
        assert _events(owner_db, pid, models_admin.JOB_ACTION[kind]) == []
    assert adapter_calls == [] and _checked_at(owner_db, pid) == before


def test_a_second_post_returns_the_active_job(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    A = org(world.a.slug)
    pid = fake_model_stack(client, as_user(client, people.owner_a), A)["provider"]
    email, _ = limited_user(owner_db, world.a.tenant, ["model:read", "model:manage"])
    for kind in (HEALTH, DISCOVER):
        first = post_job(client, as_user(client, people.owner_a), A, pid, kind)
        other = as_user(client, email)  # another caller of the same client
        second = post_job(client, other, A, pid, kind)
        assert first["id"] == second["id"]
        assert [r[3] for r in _jobs(owner_db, pid, kind)] == ["queued"]  # exactly one job
        assert execute(client, A, kind, first["id"])
        assert not execute(client, A, kind, second["id"])  # no longer queued: not run again
        third = post_job(client, other, A, pid, kind)  # once it ended, a new job
        assert third["id"] != first["id"]


def test_re_running_an_executed_job_records_no_second_event(
    client: TestClient, world: World, people: People, owner_db: Database, app_db: Database
) -> None:
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    app: Any = client.app  # Any: the FastAPI app behind the test client
    for kind in (HEALTH, DISCOVER):
        out = run_job(client, h, A, pid, kind)
        assert out["status"] == "done"
        again = models_admin.run_provider_job(
            app.state.db, app.state.integration, world.a.tenant, out["id"]
        )
        assert again is None
        assert len(_events(owner_db, pid, models_admin.JOB_ACTION[kind])) == 1
        assert job_status(client, h, A, pid, kind, out["id"]) == out


def test_a_job_closed_without_a_result_reads_as_worker_lost(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """The reaper closes an expired run-less job `done` with `stale lease`: the status route
    reports `failed` / `worker_lost`, never the raw `last_error` text."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    for kind in (HEALTH, DISCOVER):
        job = post_job(client, h, A, pid, kind)
        with owner_db.session() as o:
            o.execute(
                text(
                    "UPDATE shifde.jobs SET status = 'leased', lease_owner = 'gone', "
                    "lease_expires_at = now() - interval '1 second' WHERE id = :j"
                ),
                {"j": job["id"]},
            )
            o.commit()
        assert job_status(client, h, A, pid, kind, job["id"])["status"] == "running"
        with owner_db.session() as o:
            o.execute(text("SELECT * FROM shifde.shifde_reap_expired_leases()")).all()
            o.commit()
        assert [(r[3], r[5]) for r in _jobs(owner_db, pid, kind)] == [("done", "stale lease")]
        out = job_status(client, h, A, pid, kind, job["id"])
        assert (out["status"], out["error"], out["result"]) == ("failed", "worker_lost", None)


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
    cid = connection(client, h, body, A)["id"]
    values = provider_values(world.a.tenant, cid)
    with owner_db.session() as o:
        repository.insert_provider(o, values)
        o.commit()
    pid = str(values["id"])
    for kind, path in ((HEALTH, "health-check"), (DISCOVER, "discover-models")):
        r = client.post(f"{A}/model-providers/{pid}/{path}", headers=h)
        assert r.status_code == 422 and r.json()["code"] == "adapter_not_implemented", r.text
        assert _jobs(owner_db, pid, kind) == []


def test_the_synchronous_refusals_queue_nothing(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """`model:manage` (403) and an unknown provider (404) are answered before anything is
    queued; a `model:read` caller cannot post."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    email, uid = limited_user(owner_db, world.a.tenant, ["model:read"])
    reader = as_user(client, email)
    for kind, path in ((HEALTH, "health-check"), (DISCOVER, "discover-models")):
        r = client.post(f"{A}/model-providers/{pid}/{path}", headers=reader)
        assert r.status_code == 403, r.text
        assert _jobs(owner_db, pid, kind) == []
    grant(owner_db, world.a.tenant, uid, ["model:manage"])
    assert run_discovery(client, as_user(client, email), A, pid)[0]["model_key"] == "fake-chat"


def test_a_long_health_detail_is_truncated_in_the_audit_event(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event that holds the result keeps at most 500 characters of the detail, as the
    provider's `health_detail` does."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]

    def long(self: Any, ctx: Any, conn: Any) -> Any:  # Any: adapter call arguments
        return HealthResult(status="degraded", latency_ms=3, detail="x" * 2000)

    monkeypatch.setattr(FakeProvider, "health", long)
    out = run_job(client, h, A, pid, HEALTH)
    assert out["status"] == "done" and out["result"]["detail"] == "x" * 500
    [(_, details)] = _events(owner_db, pid, "model.provider.health_checked")
    assert details["detail"] == "x" * 500
    assert _q(
        owner_db, "SELECT health_detail FROM shifde.model_providers WHERE id = :p", p=pid
    ) == [("x" * 500,)]


def test_a_result_dated_long_before_the_job_is_still_found(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker whose clock runs days behind the database dates its event long before the job's
    `created_at`: the lookup has no time bound (issue #133), so the job reads `done`, never
    `worker_lost`, and a second run of it finds that result and records nothing more."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    app: Any = client.app  # Any: the FastAPI app behind the test client
    for kind in (HEALTH, DISCOVER):
        job = post_job(client, h, A, pid, kind)
        [(created,)] = _q(owner_db, "SELECT created_at FROM shifde.jobs WHERE id = :j", j=job["id"])
        early = created - timedelta(days=2)
        with monkeypatch.context() as m:
            m.setattr(audit_service, "utcnow", lambda early=early: early)
            assert execute(client, A, kind, job["id"])
            again = models_admin.run_provider_job(
                app.state.db, app.state.integration, world.a.tenant, UUID(job["id"])
            )
        assert again is None
        [(occurred,)] = _q(
            owner_db,
            "SELECT occurred_at FROM shifde.audit_events WHERE correlation_id = :c",
            c=f"{models_admin.JOB_PREFIX[kind]}-{job['id']}",
        )
        assert occurred < created - timedelta(minutes=5)
        assert len(_events(owner_db, pid, models_admin.JOB_ACTION[kind])) == 1
        out = job_status(client, h, A, pid, kind, job["id"])
        assert out["status"] == "done" and out["error"] is None and out["result"] is not None


def test_two_concurrent_runs_of_one_job_record_one_result(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs of one job both call the adapter (outside any transaction); the one that records
    second waits for the first (a lock on the job), finds its result and records nothing."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    app: Any = client.app  # Any: the FastAPI app behind the test client

    def run_twice(kind: str, method: str) -> None:
        both = threading.Barrier(2, timeout=30)
        calls: list[str] = []
        original = getattr(FakeProvider, method)

        def together(self: Any, ctx: Any, conn: Any) -> Any:  # Any: adapter call arguments
            calls.append(method)
            both.wait()  # both runs are past their first result lookup
            return original(self, ctx, conn)

        monkeypatch.setattr(FakeProvider, method, together)
        job = post_job(client, h, A, pid, kind)
        results: list[str | None] = []

        def run() -> None:
            results.append(
                models_admin.run_provider_job(
                    app.state.db, app.state.integration, world.a.tenant, UUID(job["id"])
                )
            )

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert results == [None, None] and calls == [method, method]
        assert len(_events(owner_db, pid, models_admin.JOB_ACTION[kind])) == 1
        assert job_status(client, h, A, pid, kind, job["id"])["status"] == "done"

    run_twice(HEALTH, "health")
    run_twice(DISCOVER, "list_models")


def test_a_unique_violation_after_another_run_recorded_the_result_records_nothing_more(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As `run_test_job`: an `IntegrityError` once another run of the job recorded its result
    ends the run without a second result; with no result recorded it is raised."""
    A, h = org(world.a.slug), as_user(client, people.owner_a)
    pid = fake_model_stack(client, h, A)["provider"]
    app: Any = client.app  # Any: the FastAPI app behind the test client
    original = models_admin.ModelService.execute_health_check

    def run(job_id: str) -> str | None:
        return models_admin.run_provider_job(
            app.state.db, app.state.integration, world.a.tenant, UUID(job_id)
        )

    def collide(self: Any, row: Any) -> None:  # Any: the service under test, the job row
        monkeypatch.setattr(models_admin.ModelService, "execute_health_check", original)
        assert run(job["id"]) is None  # another run records the result meanwhile
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    job = post_job(client, h, A, pid, HEALTH)
    monkeypatch.setattr(models_admin.ModelService, "execute_health_check", collide)
    assert run(job["id"]) is None
    assert len(_events(owner_db, pid, "model.provider.health_checked")) == 1
    assert job_status(client, h, A, pid, HEALTH, job["id"])["result"]["status"] == "healthy"

    def fail(self: Any, row: Any) -> None:  # Any: the service under test, the job row
        raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    assert execute(client, A, HEALTH, job["id"])  # the first job ends; a new one can be queued
    again = post_job(client, h, A, pid, HEALTH)
    monkeypatch.setattr(models_admin.ModelService, "execute_health_check", fail)
    with pytest.raises(IntegrityError):
        run(again["id"])
    assert len(_events(owner_db, pid, "model.provider.health_checked")) == 1
