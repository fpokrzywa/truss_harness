"""Connection tests run on the worker (ARCH M2, issue #10, B-2610): test-side shortcuts.

`POST .../connections/{id}/test` answers 202 with a queued `connection.test` job. `run_test`
posts it and executes exactly that job the way `shifde-worker` does: it leases the job only if it
is still queued (never through `shifde_claim_jobs`, so another test's job is never picked up),
runs it through `connections.service.run_test_job` on the app's own application-role database, and
finishes it with the worker's `finish_job`. It returns the status route's body.

`POST .../connections/{id}/invoke` answers 202 with a queued `connection.invoke` invocation
(issue #190); `run_invoke` posts it and executes exactly that job the same way, through
`connections.service.run_invoke_job`, and returns the status route's body.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any  # Any: JSON bodies
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.runtime import IntegrationRuntime
from shifde.connections import service as connections
from shifde.core import db
from shifde.core.db import Database
from shifde.core.errors import AppError
from shifde.core.ids import uuid7
from shifde.runtime import repository as runtime_repo


def tenant_of(client: TestClient, base: str) -> UUID:
    """The id of the client `base` (`/api/v1/orgs/{slug}`) names, from the signed-in session."""
    slug = base.rstrip("/").rsplit("/", 1)[-1]
    me = client.get("/api/auth/session")
    assert me.status_code == 200, me.text
    for m in me.json()["memberships"]:
        if m["org"]["slug"] == slug:
            return UUID(m["org"]["id"])
    raise AssertionError(f"the signed-in user is no member of {slug}")


Runner = Callable[[Database, IntegrationRuntime, UUID, UUID], str | None]


def execute_job(
    database: Database,
    runtime: IntegrationRuntime,
    tenant_id: UUID,
    job_id: UUID,
    *,
    kind: str = connections.TEST_JOB_KIND,
    runner: Runner = connections.run_test_job,
) -> bool:
    """Lease the queued `kind` job `job_id` of `tenant_id`, run it through `runner` as the worker
    does and finish it; False when it was no longer queued (nothing is run then)."""
    owner = f"test-{uuid7().hex}"
    with database.session() as s:
        db.set_tenant(s, tenant_id)
        leased = s.execute(
            text(
                "UPDATE shifde.jobs SET status = 'leased', lease_owner = :w, "
                "heartbeat_at = now(), lease_expires_at = now() + interval '5 minutes', "
                "attempts = attempts + 1 "
                "WHERE id = :id AND tenant_id = :t AND kind = :k AND status = 'queued' "
                "RETURNING id"
            ),
            {"id": job_id, "t": tenant_id, "k": kind, "w": owner},
        ).first()
        s.commit()
    if leased is None:
        return False
    error = runner(database, runtime, tenant_id, job_id)
    with database.session() as s:
        db.set_tenant(s, tenant_id)
        runtime_repo.finish_job(
            s, tenant_id, job_id, owner, "done" if error is None else "dead", last_error=error
        )
        s.commit()
    return True


def job_status(client: TestClient, h: dict[str, str], base: str, cid: str, job_id: str) -> Any:
    got = client.get(f"{base}/connections/{cid}/test-jobs/{job_id}", headers=h)
    assert got.status_code == 200, got.text
    return got.json()


def run_test(
    client: TestClient, h: dict[str, str], base: str, cid: str, *, tenant_id: UUID | None = None
) -> Any:  # Any: the status route's JSON body
    """POST the connection test (202), execute that one job, return the status route's body.
    A job a running worker loop leased first is waited for instead."""
    posted = client.post(f"{base}/connections/{cid}/test", headers=h)
    assert posted.status_code == 202, posted.text
    job = posted.json()
    assert job["connection_id"] == cid and job["status"] in ("queued", "running"), job
    app: Any = client.app  # Any: the FastAPI app behind the test client
    tenant = tenant_id or tenant_of(client, base)
    execute_job(app.state.db, app.state.integration, tenant, UUID(job["id"]))
    end = time.monotonic() + 30
    while True:
        out = job_status(client, h, base, cid, job["id"])
        if out["status"] not in ("queued", "running") or time.monotonic() > end:
            return out
        time.sleep(0.05)


def result_of(
    client: TestClient, h: dict[str, str], base: str, cid: str, *, tenant_id: UUID | None = None
) -> Any:  # Any: the test result's JSON body
    """`run_test`'s recorded result (the job must end `done`)."""
    out = run_test(client, h, base, cid, tenant_id=tenant_id)
    assert out["status"] == "done", out
    return out["result"]


# ------------------------------------------------------------------ invoke (issue #190)
def invocation_job(database: Database, tenant_id: UUID, invocation_id: UUID) -> UUID:
    """The `connection.invoke` job of the invocation `invocation_id` of `tenant_id`."""
    with database.session() as s:
        db.set_tenant(s, tenant_id)
        job_id = s.execute(
            text(
                "SELECT job_id FROM shifde.connection_invocations WHERE tenant_id = :t AND id = :i"
            ),
            {"t": tenant_id, "i": invocation_id},
        ).scalar_one()
    return UUID(str(job_id))


def invocation_status(
    client: TestClient, h: dict[str, str], base: str, cid: str, invocation_id: str
) -> Any:  # Any: the status route's JSON body
    got = client.get(f"{base}/connections/{cid}/invocations/{invocation_id}", headers=h)
    assert got.status_code == 200, got.text
    return got.json()


def execute_invocation(
    client: TestClient, base: str, invocation_id: str, *, tenant_id: UUID | None = None
) -> bool:
    """Run the queued job of `invocation_id` as the worker does (False: no longer queued)."""
    app: Any = client.app  # Any: the FastAPI app behind the test client
    tenant = tenant_id or tenant_of(client, base)
    job_id = invocation_job(app.state.db, tenant, UUID(invocation_id))
    return execute_job(
        app.state.db,
        app.state.integration,
        tenant,
        job_id,
        kind=connections.INVOKE_JOB_KIND,
        runner=connections.run_invoke_job,
    )


def run_invoke(
    client: TestClient,
    h: dict[str, str],
    base: str,
    cid: str,
    body: Any,  # Any: the invoke request's JSON body
    *,
    tenant_id: UUID | None = None,
) -> Any:  # Any: the status route's JSON body
    """POST the invoke (202), execute exactly that job, return the status route's body. A job a
    running worker loop leased first is waited for instead."""
    posted = client.post(f"{base}/connections/{cid}/invoke", json=body, headers=h)
    assert posted.status_code == 202, posted.text
    inv = posted.json()
    assert inv["connection_id"] == cid and inv["status"] in ("queued", "running"), inv
    execute_invocation(client, base, inv["id"], tenant_id=tenant_id)
    end = time.monotonic() + 30
    while True:
        out = invocation_status(client, h, base, cid, inv["id"])
        if out["status"] not in ("queued", "running") or time.monotonic() > end:
            return out
        time.sleep(0.05)


def invoke_result(
    client: TestClient,
    h: dict[str, str],
    base: str,
    cid: str,
    body: Any,  # Any: the invoke request's JSON body
    *,
    tenant_id: UUID | None = None,
) -> Any:  # Any: the stored `InvokeOut` JSON
    """`run_invoke`'s stored result (the invocation must end `succeeded`)."""
    out = run_invoke(client, h, base, cid, body, tenant_id=tenant_id)
    assert out["status"] == "succeeded", out
    return out["result"]


def _codes(cls: type[AppError]) -> dict[str, int]:
    out = {cls.code: cls.status}
    for sub in cls.__subclasses__():
        out.update(_codes(sub))
    return out


def invoke_outcome(
    client: TestClient,
    h: dict[str, str],
    base: str,
    cid: str,
    body: Any,  # Any: the invoke request's JSON body
    *,
    tenant_id: UUID | None = None,
) -> tuple[int, Any]:  # Any: JSON bodies
    """The inline invoke's answer, now through the queue: a refusal of the POST is returned as it
    is; a queued invocation is executed, then `(200, stored result)` when it succeeded, else
    `(status of its error code, status body)`, the status the inline invoke answered."""
    posted = client.post(f"{base}/connections/{cid}/invoke", json=body, headers=h)
    if posted.status_code != 202:
        return posted.status_code, posted.json()
    inv = posted.json()
    execute_invocation(client, base, inv["id"], tenant_id=tenant_id)
    out = invocation_status(client, h, base, cid, inv["id"])
    if out["status"] == "succeeded":
        return 200, out["result"]
    assert out["status"] == "failed", out
    return _codes(AppError)[out["error"]], out
