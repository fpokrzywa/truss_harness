"""Provider health checks and model discoveries run on the worker (ARCH M2, issue #47, B-2720):
test-side shortcuts.

`POST .../model-providers/{id}/health-check` and `.../discover-models` answer 202 with a queued
`model.health_check` / `model.discover` job. `run_health` / `run_discovery` post it and execute
exactly that job the way `shifde-worker` does (`conntests.execute_job` with the job's kind and
`models_admin.service.run_provider_job`), and return the status route's body.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

from fastapi.testclient import TestClient

from shifde.models_admin import service as models_admin
from tests._support.conntests import execute_job, tenant_of

ROUTES = {
    models_admin.HEALTH_JOB_KIND: ("health-check", "health-check-jobs"),
    models_admin.DISCOVER_JOB_KIND: ("discover-models", "discovery-jobs"),
}


def post_job(client: TestClient, h: dict[str, str], base: str, pid: str, kind: str) -> Any:
    """POST the provider operation of `kind`; the queued job's body (202)."""
    posted = client.post(f"{base}/model-providers/{pid}/{ROUTES[kind][0]}", headers=h)
    assert posted.status_code == 202, posted.text
    job = posted.json()
    assert job["provider_id"] == pid and job["status"] == "queued", job
    return job


def job_status(
    client: TestClient, h: dict[str, str], base: str, pid: str, kind: str, job_id: str
) -> Any:
    got = client.get(f"{base}/model-providers/{pid}/{ROUTES[kind][1]}/{job_id}", headers=h)
    assert got.status_code == 200, got.text
    return got.json()


def execute(client: TestClient, base: str, kind: str, job_id: str) -> bool:
    """Run the queued `kind` job `job_id` of the client `base` names, as the worker does."""
    app: Any = client.app  # Any: the FastAPI app behind the test client
    return execute_job(
        app.state.db,
        app.state.integration,
        tenant_of(client, base),
        UUID(job_id),
        kind=kind,
        runner=models_admin.run_provider_job,
    )


def run_job(client: TestClient, h: dict[str, str], base: str, pid: str, kind: str) -> Any:
    """POST the operation (202), execute that one job, return the status route's body."""
    job = post_job(client, h, base, pid, kind)
    assert execute(client, base, kind, job["id"])
    return job_status(client, h, base, pid, kind, job["id"])


def run_health(client: TestClient, h: dict[str, str], base: str, pid: str) -> Any:
    """The recorded health check result (the job must end `done`)."""
    out = run_job(client, h, base, pid, models_admin.HEALTH_JOB_KIND)
    assert out["status"] == "done", out
    return out["result"]


def run_discovery(client: TestClient, h: dict[str, str], base: str, pid: str) -> Any:
    """The recorded discovery result, the model list (the job must end `done`)."""
    out = run_job(client, h, base, pid, models_admin.DISCOVER_JOB_KIND)
    assert out["status"] == "done", out
    return out["result"]
