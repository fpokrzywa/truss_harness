"""Issue #373: a database error of a built-in job kind (`worker.job_failed`) or of a run
(`runtime.internal_error`) is logged without the statement's parameters, as rendered by the
product's logging setup."""

from __future__ import annotations

from typing import Any  # Any: a runner's output
from uuid import UUID

import pytest

from shifde.adapters.runtime import IntegrationRuntime
from shifde.connections import service as connections
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import state
from shifde.runtime.context import RunContext
from shifde.runtime.worker import Worker, _Job
from tests._support.dberrors import ERROR, fail_with, new_marker, rendered_logs
from tests.build.service_support import REGISTRY
from tests.runtime.test_lifecycle import Advance, C, Fake, _run, advance, fake, two

__all__ = ["advance", "fake", "two"]


def test_a_database_error_of_a_built_in_kind_logs_no_parameter(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = new_marker()
    used: list[Database] = []

    def broken(db: Database, runtime: Any, tenant_id: UUID, job_id: UUID) -> str | None:
        used.append(db)
        with db.session() as s:
            fail_with(s, marker)
        return None

    monkeypatch.setattr(connections, "run_test_job", broken)
    w = Worker(settings, worker_id="w-db-error", concurrency=1,
               runtime=IntegrationRuntime(settings=settings, registry=REGISTRY))  # fmt: skip
    job = uuid7()
    try:
        with rendered_logs(capsys, caplog, settings) as shown:
            w._work(_Job(job, uuid7(), None, connections.TEST_JOB_KIND))
            logged = shown()
    finally:
        w.db.dispose()
    assert used == [w.db]
    assert "worker.job_failed" in logged and str(job) in logged
    assert ERROR in logged and "division by zero" in logged
    assert marker not in logged


def test_a_database_error_of_a_run_logs_and_stores_no_parameter(
    two: tuple[C, C],
    advance: Advance,
    fake: type[Fake],
    owner_db: Database,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, _ = two
    marker = new_marker()

    def broken(ctx: RunContext) -> Any:
        fail_with(ctx.session, marker)

    fake.behave = staticmethod(broken)
    with rendered_logs(capsys, caplog, settings) as shown:
        assert advance(a.tenant, a.run, a.job) == "done"
        logged = shown()
    assert "runtime.internal_error" in logged and str(a.run) in logged
    assert ERROR in logged and "division by zero" in logged
    assert marker not in logged
    with owner_db.session() as o:
        row = _run(o, a.run)
    assert (row["status"], row["error_class"]) == ("failed", state.INTERNAL)
    assert row["error_message"] == "internal error (DataError)"
