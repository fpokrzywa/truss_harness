"""Findings Item 14 (REQ F-12, B-2514): with `capture_content` off a run's stored content is
scrubbed in the transaction that ends it, not only hidden on read.

Behaviour: an ended run's `run_artifacts.content` becomes `{"scrubbed": true}` (size and hash
kept) and its `tool_invocations.arguments` NULL, whether it completed, failed or was cancelled,
and on the worker's deadline branch, the reaper's follow-up transaction and the budget-denied
start; `run_steps` and `run_events` never hold the content; the result still answers redacted.
With capture on nothing is scrubbed. A child that ends under a running root keeps its output
until the root ends (the parent reads it), then both are scrubbed. The database refuses every
other change to an artifact (`run_artifacts_guard`, migration runtime_0002).

Isolation: scrubbing A's runs leaves B's content and arguments byte-for-byte unchanged, and the
scrub called in A's context with B's run id scrubs nothing, as `shifde_app` and as the schema
owner (where the client predicate alone confines it); A's own id is scrubbed (not vacuous).

Every case is driven under the app role and the schema owner (`opened`); the database is read as
the owner.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import lifecycle, state
from shifde.runtime import repository as repo
from shifde.runtime.governance import BudgetDenied, BudgetScope, PolicyBudgetHooks
from shifde.runtime.service import RunService
from tests._support.runs import add_run
from tests._support.tenants import add_tenants
from tests.build.env_support import add_published
from tests.build.service_support import REGISTRY, add_person, service_for
from tests.runtime.conftest import app_session, insert
from tests.runtime.execution import run_context
from tests.runtime.test_execution import MODES, opened
from tests.runtime.test_service import _agent_body
from tests.runtime.test_service import _client as service_client
from tests.runtime.test_worker import _worker

__all__ = ["MODES", "opened"]

SCRUBBED = {"scrubbed": True}
INPUT, MODEL_TEXT, ARGUMENT, OUTPUT = "in-7f3a", "model-9c2e", "arg-41bd", "out-d06e"


@dataclass(frozen=True)
class Seeded:
    tenant: UUID
    user: UUID
    run: UUID
    token: UUID


def _capture(o: Session, tenant: UUID, mode: str | None) -> None:
    """Set (or, with None, unset) the client's `capture_content`."""
    settings = "{}" if mode is None else f'{{"capture_content": "{mode}"}}'
    o.execute(text("UPDATE shifde.tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
              {"s": settings, "t": tenant})  # fmt: skip


def _content(o: Session, tenant: UUID, run: UUID) -> UUID:
    """A run of `tenant` with an input, a model text and a write call (with arguments), plus a
    step and an event that carry only metadata."""
    _, tool_version = add_published(o, tenant, "tools", f"tool-{uuid7().hex}", {})
    input_ref = repo.insert_artifact(o, tenant, run, "input", {"input": INPUT})
    step = uuid7()
    insert(o, "run_steps", id=step, tenant_id=tenant, run_id=run, seq=1, kind="model_call",
           name="m", status="succeeded")  # fmt: skip
    repo.insert_artifact(o, tenant, run, "intermediate", {"text": MODEL_TEXT}, step_id=step)
    invocation = uuid7()
    insert(o, "tool_invocations", id=invocation, tenant_id=tenant, run_id=run, call_ref="w1",
           tool_version_id=tool_version, side_effect_class="write_non_idempotent",
           idempotency_key=f"k-{invocation}", status="succeeded", request_hash="h",
           arguments=f'{{"item": "{ARGUMENT}"}}')  # fmt: skip
    insert(o, "run_events", id=uuid7(), tenant_id=tenant, run_id=run, seq=1, type="created")
    o.execute(text("UPDATE shifde.runs SET input_ref = :i, event_seq = 1 WHERE id = :r"),
              {"i": input_ref, "r": run})  # fmt: skip
    return input_ref


def _seed(
    o: Session, tenant: UUID, user: UUID, *, status: str = "running", **cols: Any
) -> Seeded:  # Any: run columns
    """A run of `tenant` with content; an ended one is written pending and then ended (the
    `runs` row is immutable once terminal)."""
    token = uuid7()
    ended = status in state.TERMINAL
    lease = {"lease_token": token, "started_at": utcnow()} if status == "running" else {}
    run = add_run(o, tenant, status="pending" if ended else status,
                  initiated_by_principal_id=user, **lease, **cols)  # fmt: skip
    _content(o, tenant, run)
    if ended:
        o.execute(text("UPDATE shifde.runs SET status = :s, state_version = state_version + 1, "
                       "finished_at = now() WHERE id = :r"), {"s": status, "r": run})  # fmt: skip
    return Seeded(tenant, user, run, token)


def _client(owner_db: Database, capture: str | None) -> tuple[UUID, UUID]:
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        _capture(o, tenant, capture)
        user = add_person(o, tenant, "runner", "tenant_administrator")
        o.commit()
    return tenant, user


def _artifacts(owner_db: Database, run: UUID) -> list[tuple[Any, ...]]:  # Any: row values
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT id, kind, step_id, content, size_bytes, sha256, created_at "
                 "FROM shifde.run_artifacts WHERE run_id = :r ORDER BY id"),
            {"r": run},
        )  # fmt: skip
        return [tuple(r) for r in rows]


def _arguments(owner_db: Database, run: UUID) -> list[Any]:  # Any: JSON documents
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT arguments FROM shifde.tool_invocations WHERE run_id = :r ORDER BY id"),
            {"r": run},
        )
        return [r[0] for r in rows]


def _metadata_text(owner_db: Database, run: UUID) -> str:
    """Every `run_steps` and `run_events` row of the run, as text."""
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT row_to_json(x)::text FROM shifde.run_steps x WHERE run_id = :r "
                 "UNION ALL SELECT row_to_json(e)::text FROM shifde.run_events e "
                 "WHERE run_id = :r"),
            {"r": run},
        )  # fmt: skip
        return "\n".join(r[0] for r in rows)


def _digest(doc: Any) -> tuple[int, str]:  # Any: JSON document
    raw = repo.j(doc).encode()
    return len(raw), hashlib.sha256(raw).hexdigest()


def _assert_scrubbed(owner_db: Database, run: UUID, before: list[tuple[Any, ...]]) -> None:
    after = _artifacts(owner_db, run)
    assert after and all(a[3] == SCRUBBED for a in after)
    # everything but the content is as written (the output artifact, if any, is new)
    kept = {a[0]: (a[1], a[2], a[4], a[5], a[6]) for a in after}
    for b in before:
        assert kept[b[0]] == (b[1], b[2], b[4], b[5], b[6])
    assert _arguments(owner_db, run) and all(arg is None for arg in _arguments(owner_db, run))
    meta = _metadata_text(owner_db, run)
    assert meta and not any(v in meta for v in (INPUT, MODEL_TEXT, ARGUMENT, OUTPUT))


def _service(s: Session, tenant: UUID, user: UUID, settings: Settings, **kw: Any) -> RunService:
    app = s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    sc = service_for(s, tenant, user, settings, set_context=app).sc
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    return RunService(sc, runtime, kw.get("hooks") or PolicyBudgetHooks())


def _end(how: str, s: Session, x: Seeded, settings: Settings) -> None:
    """End `x.run` (running for finalize, pending for the API cancel)."""
    row = repo.get_run(s, x.tenant, x.run)
    assert row is not None
    if how == "cancelled":
        _service(s, x.tenant, x.user, settings).cancel(x.run)
        return
    ctx = run_context(s, settings, dict(row))
    if how == "completed":
        lifecycle.finalize(ctx, state.COMPLETED, output=OUTPUT)
    else:
        lifecycle.finalize(ctx, state.FAILED, error_class="internal", message="boom")
    s.commit()


# ------------------------------------------------------------------ capture off: every ending
@pytest.mark.parametrize("how", ["completed", "failed", "cancelled"])
def test_capture_off_scrubs_an_ended_runs_content_and_keeps_sizes_and_hashes(
    how: str, opened: Any, owner_db: Database, settings: Settings
) -> None:
    tenant, user = _client(owner_db, None)  # unset: off outside development
    assert not settings.is_development
    with owner_db.session() as o:
        x = _seed(o, tenant, user, status="pending" if how == "cancelled" else "running")
        o.commit()
    before = _artifacts(owner_db, x.run)
    assert [a[3] for a in before] == [{"input": INPUT}, {"text": MODEL_TEXT}]
    with opened(tenant) as s:
        _end(how, s, x, settings)
    with owner_db.session() as o:
        assert o.execute(text("SELECT status FROM shifde.runs WHERE id = :r"),
                         {"r": x.run}).scalar_one() == how  # fmt: skip
    _assert_scrubbed(owner_db, x.run, before)
    if how == "completed":
        out = [a for a in _artifacts(owner_db, x.run) if a[1] == "output"]
        assert [(a[4], a[5]) for a in out] == [_digest({"output": OUTPUT})]
    with opened(tenant) as s:
        result = _service(s, tenant, user, settings).result(x.run)
    assert result.output is None
    if how == "completed":
        assert result.redacted is True


def test_capture_on_keeps_the_content_and_the_result(
    opened: Any, owner_db: Database, settings: Settings
) -> None:
    tenant, user = _client(owner_db, "full")
    with owner_db.session() as o:
        x = _seed(o, tenant, user)
        o.commit()
    before = _artifacts(owner_db, x.run)
    with opened(tenant) as s:
        _end("completed", s, x, settings)
    after = _artifacts(owner_db, x.run)
    assert [a for a in after if a[1] != "output"] == before
    assert [a[3] for a in after if a[1] == "output"] == [{"output": OUTPUT}]
    assert _arguments(owner_db, x.run) == [{"item": ARGUMENT}]
    with opened(tenant) as s:
        result = _service(s, tenant, user, settings).result(x.run)
    assert (result.output, result.redacted) == (OUTPUT, False)


# ------------------------------------------------------------------ family rule
def test_a_child_keeps_its_output_until_its_root_ends(
    opened: Any, owner_db: Database, settings: Settings
) -> None:
    """The parent reads a child's output after the child ended (`graph.py`, `pipeline.py`): a
    child that ends under a running root keeps it; the root's end scrubs both."""
    tenant, user = _client(owner_db, None)
    with owner_db.session() as o:
        root = _seed(o, tenant, user)
        child = _seed(o, tenant, user, parent_run_id=root.run, root_run_id=root.run, depth=1)
        o.commit()
    root_before = _artifacts(owner_db, root.run)
    child_before = _artifacts(owner_db, child.run)
    with opened(tenant) as s:
        _end("completed", s, child, settings)
    kept = _artifacts(owner_db, child.run)
    assert [a for a in kept if a[1] != "output"] == child_before
    assert [a[3] for a in kept if a[1] == "output"] == [{"output": OUTPUT}]
    assert _arguments(owner_db, child.run) == [{"item": ARGUMENT}]
    assert _artifacts(owner_db, root.run) == root_before
    with opened(tenant) as s:
        _end("completed", s, root, settings)
    _assert_scrubbed(owner_db, root.run, root_before)
    _assert_scrubbed(owner_db, child.run, child_before)


def test_a_child_that_ends_after_its_root_is_scrubbed_at_once(
    opened: Any, owner_db: Database, settings: Settings
) -> None:
    tenant, user = _client(owner_db, None)
    with owner_db.session() as o:
        root = _seed(o, tenant, user, status="cancelled")
        child = _seed(o, tenant, user, parent_run_id=root.run, root_run_id=root.run, depth=1)
        o.commit()
    before = _artifacts(owner_db, child.run)
    with opened(tenant) as s:
        _end("failed", s, child, settings)
    _assert_scrubbed(owner_db, child.run, before)


# ------------------------------------------------------------------ recovery and reconcile
def test_the_workers_deadline_branch_scrubs(
    opened: Any, owner_db: Database, settings: Settings
) -> None:
    tenant, user = _client(owner_db, None)
    job = uuid7()
    with owner_db.session() as o:
        x = _seed(o, tenant, user, status="pending", deadline_at=utcnow() - timedelta(seconds=1))
        insert(o, "jobs", id=job, tenant_id=tenant, kind="run.advance", run_id=x.run,
               dedupe_key=f"run:{x.run}:1")  # fmt: skip
        o.commit()
    before = _artifacts(owner_db, x.run)
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    with opened(tenant) as s:
        outcome = lifecycle._advance(
            s, None, settings, runtime, PolicyBudgetHooks(), tenant, x.run, "w-test",
            threading.Event(), threading.Event(), job,
        )  # fmt: skip
    assert outcome == "done"
    _assert_scrubbed(owner_db, x.run, before)


def test_the_workers_parent_ended_branch_scrubs(
    opened: Any, owner_db: Database, settings: Settings
) -> None:
    tenant, user = _client(owner_db, None)
    job = uuid7()
    with owner_db.session() as o:
        pipe_id, pipe_version = add_published(o, tenant, "pipelines", f"pipe-{uuid7().hex}", {})
        pipe = add_run(o, tenant, kind="pipeline", pipeline_id=pipe_id,
                       pipeline_version_id=pipe_version, status="failed",
                       finished_at=utcnow())  # fmt: skip
        x = _seed(o, tenant, user, status="pending", parent_run_id=pipe, root_run_id=pipe,
                  depth=1)  # fmt: skip
        insert(o, "jobs", id=job, tenant_id=tenant, kind="run.advance", run_id=x.run,
               dedupe_key=f"run:{x.run}:1")  # fmt: skip
        o.commit()
    before = _artifacts(owner_db, x.run)
    runtime = IntegrationRuntime(settings=settings, registry=REGISTRY)
    with opened(tenant) as s:
        outcome = lifecycle._advance(
            s, None, settings, runtime, PolicyBudgetHooks(), tenant, x.run, "w-test",
            threading.Event(), threading.Event(), job,
        )  # fmt: skip
    assert outcome == "done"
    _assert_scrubbed(owner_db, x.run, before)


def test_a_reaped_run_is_scrubbed_in_the_follow_up_transaction(
    owner_db: Database, settings: Settings
) -> None:
    """The reaper fails the run inside the database (attempts exhausted); `_audit_reaped`
    scrubs it in the run's own client."""
    tenant, user = _client(owner_db, None)
    job = uuid7()
    with owner_db.session() as o:
        x = _seed(o, tenant, user)
        insert(o, "jobs", id=job, tenant_id=tenant, kind="run.advance", run_id=x.run,
               dedupe_key=f"run:{x.run}:1", status="leased", lease_owner="w-dead",
               lease_expires_at=utcnow() - timedelta(seconds=1), attempts=3,
               max_attempts=3)  # fmt: skip
        o.execute(text("UPDATE shifde.runs SET lease_job_id = :j WHERE id = :r"),
                  {"j": job, "r": x.run})  # fmt: skip
        o.commit()
    before = _artifacts(owner_db, x.run)
    w = _worker(settings, "w-reaper")
    try:
        w._reap()
    finally:
        w._executor.shutdown()
        w.db.dispose()
    with owner_db.session() as o:
        assert o.execute(text("SELECT status FROM shifde.runs WHERE id = :r"),
                         {"r": x.run}).scalar_one() == "failed"  # fmt: skip
    _assert_scrubbed(owner_db, x.run, before)


class _Denying(PolicyBudgetHooks):
    def preflight(self, session: Session, scope: BudgetScope) -> None:
        raise BudgetDenied("over budget")


def test_a_budget_denied_start_scrubs_its_input(
    opened: Any, owner_db: Database, settings: Settings
) -> None:
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        c = service_client(o, tenant)
        _capture(o, tenant, None)
        o.commit()
    with opened(tenant) as s:
        out, created = _service(s, tenant, c.admin, settings, hooks=_Denying()).create(
            _agent_body(c, input=INPUT), None
        )
    assert created and out.status == "failed"
    arts = _artifacts(owner_db, out.id)
    assert [a[1] for a in arts] == ["input"] and arts[0][3] == SCRUBBED
    with owner_db.session() as o:
        [error_class] = o.execute(
            text("SELECT error_class FROM shifde.runs WHERE id = :r"), {"r": out.id}
        ).scalars()
    assert error_class == state.BUDGET_DENIED


# ------------------------------------------------------------------ the database guard
def test_the_database_allows_only_the_placeholder_on_an_ended_runs_artifact(
    app_db: Database, owner_db: Database
) -> None:
    """`run_artifacts_guard` (runtime_0002): DELETE, any other column, any other content and the
    placeholder on a run that has not ended are refused, as `shifde_app` and as the owner."""
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        user = add_person(o, tenant, "runner", "tenant_administrator")
        active = _seed(o, tenant, user)
        ended = _seed(o, tenant, user, status="failed")
        o.commit()
    refused = [
        ("DELETE FROM shifde.run_artifacts WHERE run_id = :r", ended.run),
        ("UPDATE shifde.run_artifacts SET content = '{\"x\": 1}' WHERE run_id = :r", ended.run),
        ("UPDATE shifde.run_artifacts SET content = '{\"scrubbed\": true}', size_bytes = 1 "
         "WHERE run_id = :r", ended.run),
        ("UPDATE shifde.run_artifacts SET content = '{\"scrubbed\": true}' WHERE run_id = :r",
         active.run),
    ]  # fmt: skip
    sessions = (lambda: app_session(app_db, tenant), owner_db.session)
    for opener in sessions:
        for sql, run in refused:
            with opener() as s:
                with pytest.raises(DBAPIError) as err:
                    s.execute(text(sql), {"r": run})
                assert getattr(err.value.orig, "sqlstate", None) == "42501", sql
    with app_session(app_db, tenant) as s:
        repo.scrub_run_content(s, tenant, ended.run)
        s.commit()
    assert all(a[3] == SCRUBBED for a in _artifacts(owner_db, ended.run))
    assert _arguments(owner_db, ended.run) == [None]


def test_the_database_allows_only_clearing_an_ended_runs_arguments(
    app_db: Database, owner_db: Database
) -> None:
    """`tool_invocations_arguments_guard` (runtime_0002): a non-NULL value, NULL on a run that
    has not ended and NULL together with another column are refused, as `shifde_app` and as the
    owner; the runtime's status updates (which do not name `arguments`) still work."""
    tenant = uuid7()
    with owner_db.session() as o:
        add_tenants(o, tenant)
        user = add_person(o, tenant, "runner", "tenant_administrator")
        active = _seed(o, tenant, user)
        ended = _seed(o, tenant, user, status="failed")
        o.commit()
    refused = [
        ("UPDATE shifde.tool_invocations SET arguments = '{\"x\": 1}' WHERE run_id = :r",
         ended.run),
        ("UPDATE shifde.tool_invocations SET arguments = NULL WHERE run_id = :r", active.run),
        ("UPDATE shifde.tool_invocations SET arguments = NULL, status = 'failed' "
         "WHERE run_id = :r", ended.run),
    ]  # fmt: skip
    sessions = (lambda: app_session(app_db, tenant), owner_db.session)
    for opener in sessions:
        for sql, run in refused:
            with opener() as s:
                with pytest.raises(DBAPIError) as err:
                    s.execute(text(sql), {"r": run})
                assert getattr(err.value.orig, "sqlstate", None) == "42501", sql
    assert ARGUMENT in repr(_arguments(owner_db, active.run) + _arguments(owner_db, ended.run))
    with app_session(app_db, tenant) as s:
        s.execute(
            text("UPDATE shifde.tool_invocations SET status = 'failed' WHERE run_id = :r"),
            {"r": active.run},
        )
        repo.scrub_run_content(s, tenant, ended.run)
        s.commit()
    assert _arguments(owner_db, ended.run) == [None]
    assert ARGUMENT in repr(_arguments(owner_db, active.run))


# ------------------------------------------------------------------ cross-client
def _snapshot(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    with owner_db.session() as o:
        arts = o.execute(
            text("SELECT id, content::text, size_bytes, sha256 FROM shifde.run_artifacts "
                 "WHERE tenant_id = :t ORDER BY id"), {"t": tenant},
        ).all()  # fmt: skip
        args = o.execute(
            text("SELECT id, arguments::text FROM shifde.tool_invocations WHERE tenant_id = :t "
                 "ORDER BY id"), {"t": tenant},
        ).all()  # fmt: skip
    return tuple(map(tuple, arts)), tuple(map(tuple, args))


def test_scrubbing_as_a_never_touches_bs_rows(
    opened: Any, owner_db: Database, settings: Settings
) -> None:
    a_tenant, a_user = _client(owner_db, None)
    b_tenant, b_user = _client(owner_db, None)
    with owner_db.session() as o:
        a = _seed(o, a_tenant, a_user)
        a_ended = _seed(o, a_tenant, a_user, status="failed")
        b_ended = _seed(o, b_tenant, b_user, status="failed")
        o.commit()
    b_before = _snapshot(owner_db, b_tenant)
    assert INPUT in repr(b_before) and ARGUMENT in repr(b_before)
    with opened(a_tenant) as s:
        _end("completed", s, a, settings)
    assert _snapshot(owner_db, b_tenant) == b_before
    a_before = _snapshot(owner_db, a_tenant)
    with opened(a_tenant) as s:  # A's context naming B's ended run, then an unknown id
        repo.scrub_run_content(s, a_tenant, b_ended.run)
        repo.scrub_run_content(s, a_tenant, uuid7())
        s.commit()
    assert _snapshot(owner_db, b_tenant) == b_before
    assert _snapshot(owner_db, a_tenant) == a_before
    with opened(a_tenant) as s:  # not vacuous: A's own ended run is scrubbed
        repo.scrub_run_content(s, a_tenant, a_ended.run)
        s.commit()
    assert all(x[3] == SCRUBBED for x in _artifacts(owner_db, a_ended.run))
    assert _snapshot(owner_db, b_tenant) == b_before
