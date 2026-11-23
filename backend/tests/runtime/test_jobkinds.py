"""SP5-2 (design section 3, R1): the worker's job-kind registry.

`JobKindRegistry.of` refuses a kind of the worker's own (`run.advance`, `connection.*`,
`model.*`) and is immutable. A `Worker` given a registry runs a registered kind after its
built-in kinds, in the job's own client: None ends the job `done`, a code ends it `dead` with that
code; an unregistered kind still ends `dead` with `unknown job kind`. The composition root
`shifde.worker_main` registers `appgen.draft` -> `appgen.service.run_draft_job`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from shifde import worker_main
from shifde.adapters.runtime import IntegrationRuntime
from shifde.appgen import service as appgen
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import worker as worker_mod
from shifde.runtime.jobkinds import JobKindRegistry
from shifde.runtime.worker import Worker
from tests.build.service_support import REGISTRY
from tests.runtime.conftest import insert
from tests.tenancy.conftest import World, world

__all__ = ["world"]


def _none(*args: Any) -> str | None:
    return None


@pytest.mark.parametrize("kind", ["run.advance", "connection.x", "model.x"])
def test_a_built_in_kind_cannot_be_registered(kind: str) -> None:
    with pytest.raises(ValueError, match="built-in"):
        JobKindRegistry.of({kind: _none})


def test_the_registry_is_immutable() -> None:
    handlers = {"demo.kind": _none}
    registry = JobKindRegistry.of(handlers)
    handlers["other.kind"] = _none  # the source mapping is copied
    assert list(registry) == ["demo.kind"] and len(registry) == 1
    with pytest.raises(TypeError):
        registry["other.kind"] = _none  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        registry._handlers["other.kind"] = _none  # type: ignore[index]
    assert "other.kind" not in registry


def test_the_composition_root_registers_the_draft_job() -> None:
    handlers = worker_main.job_handlers()
    assert dict(handlers) == {appgen.DRAFT_JOB_KIND: appgen.run_draft_job}
    assert appgen.DRAFT_JOB_KIND == "appgen.draft"


@pytest.fixture
def running(settings: Settings) -> Iterator[Callable[..., Worker]]:
    started: list[tuple[Worker, threading.Thread]] = []

    def start(worker_id: str, job_handlers: JobKindRegistry | None) -> Worker:
        runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
        w = Worker(settings, worker_id=worker_id, runtime=runtime, concurrency=1,
                   lease_seconds=2, poll_ms=50, reap_seconds=3600.0,
                   job_handlers=job_handlers)  # fmt: skip
        t = threading.Thread(target=w.run, daemon=True, name=f"test-{worker_id}")
        t.start()
        started.append((w, t))
        return w

    yield start
    for w, t in started:
        w.request_stop()
        t.join(60)
        assert not t.is_alive(), "a worker loop did not stop"


def _job(owner_db: Database, tenant: UUID, kind: str) -> UUID:
    job = uuid7()
    with owner_db.session() as o:
        insert(o, "jobs", id=job, tenant_id=tenant, kind=kind, dedupe_key=f"{kind}:{job}")
        o.commit()
    return job


def _until_ended(owner_db: Database, job: UUID) -> tuple[Any, ...]:  # Any: row values
    end = time.monotonic() + 30
    while time.monotonic() < end:
        with owner_db.session() as o:
            row = o.execute(
                text("SELECT status, last_error FROM shifde.jobs WHERE id = :j"), {"j": job}
            ).one()
        if row[0] in ("done", "dead"):
            return tuple(row)
        time.sleep(0.05)
    raise AssertionError("the job did not end in time")


def test_a_registered_kind_runs_in_its_own_client_and_its_code_ends_the_job(
    world: World, owner_db: Database, running: Callable[..., Worker]
) -> None:
    ran: list[tuple[UUID, UUID]] = []

    def ok(db: Database, rt: IntegrationRuntime, tenant: UUID, job: UUID) -> str | None:
        ran.append((tenant, job))
        return None

    def refused(db: Database, rt: IntegrationRuntime, tenant: UUID, job: UUID) -> str | None:
        ran.append((tenant, job))
        return "access_revoked"

    done = _job(owner_db, world.a.tenant, "demo.ok")
    dead = _job(owner_db, world.b.tenant, "demo.refused")
    running("w-kinds", JobKindRegistry.of({"demo.ok": ok, "demo.refused": refused}))
    assert _until_ended(owner_db, done) == ("done", None)
    assert _until_ended(owner_db, dead) == ("dead", "access_revoked")
    assert sorted(ran) == sorted([(world.a.tenant, done), (world.b.tenant, dead)])


def test_an_unregistered_kind_still_ends_dead_unrun(
    world: World, owner_db: Database, running: Callable[..., Worker]
) -> None:
    ran: list[object] = []
    job = _job(owner_db, world.a.tenant, "mystery.kind")
    running("w-unknown", JobKindRegistry.of({"demo.ok": lambda *a: ran.append(a)}))
    assert _until_ended(owner_db, job) == ("dead", worker_mod.UNKNOWN_KIND)
    assert ran == []
