"""Issue #384: a database error of a built-in job kind (`worker.job_failed`) or of a run
(`runtime.internal_error`) is logged without the values the server named in its own message or
DETAIL, as rendered by the product's logging setup."""

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
from tests._support.dberrors import (
    BAD_UUID,
    UNIQUE_VIOLATION,
    Failure,
    fail_naming,
    new_marker,
    rendered_logs,
)
from tests.build.service_support import REGISTRY
from tests.runtime.test_lifecycle import Advance, C, Fake, _run, advance, fake, two

__all__ = ["advance", "fake", "two"]

both = pytest.mark.parametrize("failure", [BAD_UUID, UNIQUE_VIOLATION], ids=["uuid", "unique"])


@both
def test_a_database_error_of_a_built_in_kind_logs_no_value_the_server_named(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    failure: Failure,
) -> None:
    marker = new_marker()
    named: list[str] = []
    used: list[Database] = []

    def broken(db: Database, runtime: Any, tenant_id: UUID, job_id: UUID) -> str | None:
        used.append(db)
        with db.session() as s:
            fail_naming(s, marker, failure, named)
        return None

    monkeypatch.setattr(connections, "run_test_job", broken)
    w = Worker(settings, worker_id="w-db-values", concurrency=1,
               runtime=IntegrationRuntime(settings=settings, registry=REGISTRY))  # fmt: skip
    job = uuid7()
    try:
        with rendered_logs(capsys, caplog, settings) as shown:
            w._work(_Job(job, uuid7(), None, connections.TEST_JOB_KIND))
            logged = shown()
    finally:
        w.db.dispose()
    assert used == [w.db] and any(marker in said for said in named)
    assert "worker.job_failed" in logged and str(job) in logged
    failure.check_log(logged, marker)


@both
def test_a_database_error_of_a_run_logs_and_stores_no_value_the_server_named(
    two: tuple[C, C],
    advance: Advance,
    fake: type[Fake],
    owner_db: Database,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    failure: Failure,
) -> None:
    a, _ = two
    marker = new_marker()
    named: list[str] = []

    def broken(ctx: RunContext) -> Any:
        fail_naming(ctx.session, marker, failure, named)

    fake.behave = staticmethod(broken)
    with rendered_logs(capsys, caplog, settings) as shown:
        assert advance(a.tenant, a.run, a.job) == "done"
        logged = shown()
    assert any(marker in said for said in named)
    assert "runtime.internal_error" in logged and str(a.run) in logged
    failure.check_log(logged, marker)
    with owner_db.session() as o:
        row = _run(o, a.run)
    assert (row["status"], row["error_class"]) == ("failed", state.INTERNAL)
    name = failure.error.rsplit(".", 1)[-1]
    assert row["error_message"] == f"internal error ({name})"
