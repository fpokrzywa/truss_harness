"""`shifde-admin maintenance delete` (issue #126, SP4-8, REQ F-11, B-2783): old, ended run families
are deleted client by client as the application role, through the guards of migration
`runtime_0006`.

Behaviour: `--dry-run` reports the counts and changes nothing; a real run deletes an old ended
family's rows from all nine run tables and keeps its usage events; running runs, recently ended
runs, an ended child of a running root and an old family with a recent member are untouched; a
stored `retain_days` longer than `--older-than` wins; a client on legal hold is skipped and still
gets its event; each pin (held reservation, pending approval, queued or leased job, a case run of
a passed evaluation a delivery plan uses, of a release that is not withdrawn) keeps the whole
family; one audit event per client and every chain still verifies; a second run finds nothing
left; invalid `--older-than` values and a privileged role are refused.

Guards: as `shifde_app` in the client's own context and as the schema owner, a delete without a
cutoff, of a run that is not terminal or ended after the cutoff, of a pending approval, a queued
job or a held reservation is refused with 42501, and so is a row with a NULL run_id. Since
`runtime_0008` (issue #174, B-3987) the guard also refuses a client on legal hold (any data class)
and a cutoff later than now() minus the run retention floor, which is pinned equal to the floor the
command applies; the downgrade restores the runtime_0006 body exactly. Since `runtime_0009` (issue
#347, B-3993) it also refuses a cutoff later than now() minus the client's effective run retention
(its stored `retain_days` of either run class, or the 90-day default, pinned equal to the rule of
`effective_days`); the downgrade restores the runtime_0008 body exactly. The command takes its
cutoff from the database clock (B-3994). A case run of an evaluation that has not ended pins its
family.

Isolation: with A deleted and B on legal hold (or with a longer stored `retain_days`), every row of
B in the nine tables, `usage_events` and `audit_events` is byte-for-byte unchanged apart from B's
own new event, and A's rows are really gone (the check is not vacuous); as `shifde_app` with A's
context and a valid cutoff, deleting B's run deletes 0 rows and B is unchanged.

Data is seeded and read as the schema owner; the command itself connects as `shifde_app`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from shifde.audit import service as audit
from shifde.cli import main as cli_main
from shifde.cli.maintenance import DELETE_TABLES, RUN_CLASSES, _delete_pass
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config
from shifde.core.settings import Settings
from shifde.governance.validation import RETENTION_DEFAULTS, RETENTION_FLOORS
from shifde.runtime import repository as repo
from tests._support.runs import add_run
from tests._support.tenants import add_tenant
from tests.build.env_support import add_published
from tests.conftest import DB_PREFIX, _create_database, _drop_database, _with_db
from tests.delivery.releases import make_release
from tests.runtime.conftest import insert

ACTION = "maintenance.retention.deleted"
OLD, RECENT = 200, 1  # days since the run ended
RUN_TABLES = (*DELETE_TABLES, "runs")
CUTOFF = "SELECT set_config('shifde.retention_delete_cutoff', :c, true)"


@dataclass(frozen=True)
class Client:
    tenant: UUID
    slug: str


@pytest.fixture
def cli(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(cli_main, "_settings", lambda: settings)
    return CliRunner()


def _delete(runner: CliRunner, *args: str) -> Any:  # Any: click Result
    return runner.invoke(cli_main.app, ["maintenance", "delete", *args])


def _client(owner_db: Database) -> Client:
    with owner_db.session() as o:
        tenant = add_tenant(o)
        slug = o.execute(text("SELECT slug FROM shifde.tenants WHERE id = :t"), {"t": tenant})
        client = Client(tenant, slug.scalar_one())
        o.commit()
    return client


def _retention(owner_db: Database, tenant: UUID, data_class: str, days: int, hold: bool) -> None:
    with owner_db.session() as o:
        o.execute(
            text(
                "INSERT INTO shifde.retention_policies (tenant_id, data_class, retain_days, "
                "legal_hold) VALUES (:t, :c, :d, :h)"
            ),
            {"t": tenant, "c": data_class, "d": days, "h": hold},
        )
        o.commit()


def _rows(o: Session, tenant: UUID, run: UUID, pins: dict[str, str]) -> None:
    """One row of `run` in each of the eight tables that hang off it, plus a usage event. `pins`
    overrides the status of the approval, job or reservation (decided, done and settled)."""
    ws, project = o.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": run}
    ).one()
    _, tool_version = add_published(o, tenant, "tools", f"tool-{uuid7().hex}", {})
    _, ontology_version = add_published(o, tenant, "ontologies", f"onto-{uuid7().hex}", {})
    insert(o, "run_events", id=uuid7(), tenant_id=tenant, run_id=run, seq=1, type="note")
    repo.insert_artifact(o, tenant, run, "input", {"input": f"in-{run.hex}"})
    invocation = uuid7()
    insert(o, "tool_invocations", id=invocation, tenant_id=tenant, run_id=run, call_ref="w1",
           tool_version_id=tool_version, side_effect_class="write_non_idempotent",
           idempotency_key=f"k-{invocation}", status="succeeded", request_hash="h",
           arguments=f'{{"item": "arg-{run.hex}"}}')  # fmt: skip
    # the step names the invocation: run_steps has to go first (fk_run_steps__tool_invocation)
    insert(o, "run_steps", id=uuid7(), tenant_id=tenant, run_id=run, seq=1, kind="tool_call",
           name="t", status="succeeded", tool_invocation_id=invocation)  # fmt: skip
    approval = pins.get("approval", "approved")
    decider = uuid7() if approval in ("approved", "rejected") else None
    insert(o, "approvals", id=uuid7(), tenant_id=tenant, run_id=run, workspace_id=ws,
           project_id=project, call_ref="w1", reason="tool_side_effect", requested_by=uuid7(),
           expires_at=utcnow() + timedelta(hours=1), status=approval, decided_by=decider)  # fmt: skip
    insert(o, "jobs", id=uuid7(), tenant_id=tenant, kind="run", run_id=run,
           status=pins.get("job", "done"), dedupe_key=f"run-{run}")  # fmt: skip
    insert(o, "ontology_query_lineage", id=uuid7(), tenant_id=tenant, principal_id=uuid7(),
           principal_type="user", ontology_id=uuid7(), ontology_version_id=ontology_version,
           run_id=run, channel="agent", object_types=["x"], connection_ids=[],
           compiled_query_hashes=[], duration_ms=1, outcome="success")  # fmt: skip
    insert(o, "budget_reservations", id=uuid7(), tenant_id=tenant, run_id=run, step_key="s1",
           budget_ids=[], amount=1, status=pins.get("reservation", "settled"),
           expires_at=utcnow() + timedelta(hours=1))  # fmt: skip
    insert(o, "usage_events", id=uuid7(), tenant_id=tenant, event_key=f"run-{run}",
           event_type="run", run_id=run, root_run_id=run)  # fmt: skip


def _run(
    owner_db: Database,
    tenant: UUID,
    *,
    ended_days_ago: int | None,
    root: UUID | None = None,
    pins: dict[str, str] | None = None,
) -> UUID:
    """A run of `tenant` with a row in every run table: running when `ended_days_ago` is None,
    else cancelled that many days ago. With `root` it is a child of that run."""
    tree = {} if root is None else {"root_run_id": root, "parent_run_id": root, "depth": 1}
    with owner_db.session() as o:
        if ended_days_ago is None:
            run = add_run(o, tenant, status="running", lease_token=uuid7(),
                          started_at=utcnow(), **tree)  # fmt: skip
            _rows(o, tenant, run, pins or {})
        else:
            run = add_run(o, tenant, status="pending", **tree)
            _rows(o, tenant, run, pins or {})
            o.execute(
                text(
                    "UPDATE shifde.runs SET status = 'cancelled', "
                    "state_version = state_version + 1, finished_at = :f WHERE id = :r"
                ),
                {"f": utcnow() - timedelta(days=ended_days_ago), "r": run},
            )
        o.commit()
    return run


def _present(owner_db: Database, run: UUID) -> dict[str, int]:
    """Rows of `run` per run table (and its usage events)."""
    with owner_db.session() as o:
        counts = {
            t: o.execute(
                text(
                    f"SELECT count(*) FROM shifde.{t} WHERE {'id' if t == 'runs' else 'run_id'} = :r"
                ),
                {"r": run},
            ).scalar_one()
            for t in (*RUN_TABLES, "usage_events")
        }
    return counts


FULL = {t: 1 for t in (*RUN_TABLES, "usage_events")}
GONE = {**{t: 0 for t in RUN_TABLES}, "usage_events": 1}


def _tenant_rows(owner_db: Database, tenant: UUID) -> dict[str, list[str]]:
    """Every row of `tenant` in the run tables, usage_events and audit_events (apart from this
    command's events), as `to_jsonb` text ordered by id."""
    with owner_db.session() as o:
        rows = {
            t: list(
                o.execute(
                    text(
                        f"SELECT to_jsonb(x)::text FROM shifde.{t} x WHERE x.tenant_id = :t "
                        f"{'AND x.action <> :a ' if t == 'audit_events' else ''}ORDER BY x.id"
                    ),
                    {"t": tenant, "a": ACTION},
                ).scalars()
            )
            for t in (*RUN_TABLES, "usage_events", "audit_events")
        }
    return rows


def _events(owner_db: Database, tenant: UUID) -> list[Any]:  # Any: audit rows
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT action, outcome, details FROM shifde.audit_events "
                    "WHERE tenant_id = :t AND action = :a ORDER BY chain_seq"
                ),
                {"t": tenant, "a": ACTION},
            ).all()
        )


def _line(output: str, slug: str) -> str:
    lines = [ln for ln in output.splitlines() if ln.startswith(f"{slug}:")]
    assert len(lines) == 1, output
    return lines[0]


def _counts(runs: int, kept: int, n: int) -> str:
    """The start of a client's line: `runs` deleted, `kept` kept, `n` rows in each table (no
    assist rows)."""
    tables = ", ".join(f"{t} {n}" for t in DELETE_TABLES)
    return (
        f"runs {runs}, runs kept {kept}, {tables}, assist_requests 0, assist_proposals 0 (cutoff "
    )


def test_dry_run_reports_counts_and_changes_nothing(cli: CliRunner, owner_db: Database) -> None:
    """(a)"""
    c = _client(owner_db)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    before = _tenant_rows(owner_db, c.tenant)
    result = _delete(cli, "--older-than", "30", "--dry-run")
    assert result.exit_code == 0, result.output
    line = _line(result.output, c.slug)
    assert line.startswith(f"{c.slug}: {_counts(1, 0, 1)}")
    assert result.output.rstrip().endswith("dry run: nothing changed")
    assert _tenant_rows(owner_db, c.tenant) == before
    assert _present(owner_db, run) == FULL
    assert _events(owner_db, c.tenant) == []
    # a real run right after reports the same counts
    real = _delete(cli, "--older-than", "30")
    assert real.exit_code == 0, real.output
    assert _line(real.output, c.slug).split(" (cutoff")[0] == line.split(" (cutoff")[0]


def test_a_real_run_deletes_an_old_family_from_all_nine_tables(
    cli: CliRunner, owner_db: Database
) -> None:
    """(b)"""
    c = _client(owner_db)
    root = _run(owner_db, c.tenant, ended_days_ago=OLD)
    child = _run(owner_db, c.tenant, ended_days_ago=OLD, root=root)
    before = _tenant_rows(owner_db, c.tenant)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert "dry run" not in result.output
    assert _line(result.output, c.slug).startswith(f"{c.slug}: {_counts(2, 0, 2)}")
    assert _present(owner_db, root) == GONE
    assert _present(owner_db, child) == GONE
    after = _tenant_rows(owner_db, c.tenant)
    assert all(after[t] == [] for t in RUN_TABLES)
    # usage events keep their run id; audit events are untouched apart from the new event
    assert after["usage_events"] == before["usage_events"]
    assert {r for r in after["usage_events"] if str(root) in r or str(child) in r}
    assert after["audit_events"] == before["audit_events"]
    assert len(_events(owner_db, c.tenant)) == 1


def test_running_recent_and_partly_recent_families_are_untouched(
    cli: CliRunner, owner_db: Database
) -> None:
    """(c)"""
    c = _client(owner_db)
    running = _run(owner_db, c.tenant, ended_days_ago=None)
    recent = _run(owner_db, c.tenant, ended_days_ago=RECENT)
    child = _run(owner_db, c.tenant, ended_days_ago=OLD, root=running)
    mixed_root = _run(owner_db, c.tenant, ended_days_ago=OLD)
    mixed_child = _run(owner_db, c.tenant, ended_days_ago=RECENT, root=mixed_root)
    old = _run(owner_db, c.tenant, ended_days_ago=OLD)
    kept = (running, recent, child, mixed_root, mixed_child)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    # the old root of the mixed family is eligible on its own and kept with its family
    assert _line(result.output, c.slug).startswith(f"{c.slug}: {_counts(1, 1, 1)}")
    assert all(_present(owner_db, r) == FULL for r in kept)
    assert _present(owner_db, old) == GONE


def test_stored_retention_longer_than_older_than_keeps_the_run(
    cli: CliRunner, owner_db: Database
) -> None:
    """(d)"""
    c = _client(owner_db)
    _retention(owner_db, c.tenant, "run_steps_content", 400, False)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug).startswith(f"{c.slug}: {_counts(0, 0, 0)}")
    assert _present(owner_db, run) == FULL
    (event,) = _events(owner_db, c.tenant)
    assert event.details["effective_days"] == 400


def test_a_client_on_legal_hold_is_skipped_and_gets_its_event(
    cli: CliRunner, owner_db: Database
) -> None:
    """(e)"""
    c = _client(owner_db)
    _retention(owner_db, c.tenant, "usage_events", 395, True)  # a hold on any data class
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug) == f"{c.slug}: skipped (legal hold)"
    assert _present(owner_db, run) == FULL
    (event,) = _events(owner_db, c.tenant)
    assert (event.action, event.outcome) == (ACTION, "success")
    assert event.details["legal_hold"] is True
    assert event.details["runs"] == event.details["runs_kept"] == 0
    assert all(event.details[t] == 0 for t in DELETE_TABLES)


@pytest.mark.parametrize(
    "pin", [{"reservation": "held"}, {"approval": "pending"}, {"job": "queued"}, {"job": "leased"}]
)
def test_a_pinned_child_keeps_its_whole_family(
    cli: CliRunner, owner_db: Database, pin: dict[str, str]
) -> None:
    """(f) a held reservation, a pending approval, a queued or leased job"""
    c = _client(owner_db)
    root = _run(owner_db, c.tenant, ended_days_ago=OLD)
    pinned = _run(owner_db, c.tenant, ended_days_ago=OLD, root=root, pins=pin)
    old = _run(owner_db, c.tenant, ended_days_ago=OLD)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug).startswith(f"{c.slug}: {_counts(1, 2, 1)}")
    assert _present(owner_db, root) == FULL
    assert _present(owner_db, pinned) == FULL
    assert _present(owner_db, old) == GONE


def _evaluation(
    owner_db: Database,
    tenant: UUID,
    run: UUID,
    *,
    withdrawn: bool,
    plan: bool,
    status: str = "passed",
) -> None:
    """An evaluation run (`status`, passed unless given) whose case run is `run`, of a release in
    the run's workspace (withdrawn if asked), named by a delivery plan if `plan`."""
    with owner_db.session() as o:
        ws = o.execute(text("SELECT workspace_id FROM shifde.runs WHERE id = :r"), {"r": run})
        workspace = ws.scalar_one()
    rel = make_release(owner_db, tenant, workspace, evaluation=None, withdrawn=withdrawn)
    evaluation, target, artifact = uuid7(), uuid7(), uuid7()
    with owner_db.session() as o:
        set_tenant(o, tenant)
        o.execute(
            text(
                "INSERT INTO shifde.evaluation_runs (tenant_id, id, release_id, release_digest, "
                "dataset_id, dataset_digest, thresholds, case_runs, status, requested_by) VALUES "
                "(:t, :i, :r, :rd, :d, :dd, '{}'::jsonb, CAST(:cr AS jsonb), :s, :t)"
            ),
            {"t": tenant, "i": evaluation, "r": rel.id, "rd": rel.digest, "d": rel.dataset_id,
             "dd": rel.dataset_digest, "s": status,
             "cr": f'[{{"case_id": "c1", "run_id": "{run}"}}]'},
        )  # fmt: skip
        if plan:
            insert(o, "delivery_targets", tenant_id=tenant, id=target, workspace_id=workspace,
                   name="t", platform_kind="databricks", address="https://x", environment="dev",
                   bindings="{}", credential_ref="cred", status="archived",
                   created_by=uuid7())  # fmt: skip
            insert(o, "delivery_artifacts", tenant_id=tenant, id=artifact,
                   target_kind="databricks.app/v1", digest="a" * 64, size=1, content=b"x",
                   created_by=uuid7())  # fmt: skip
            insert(o, "delivery_plans", tenant_id=tenant, id=uuid7(), target_id=target,
                   target_revision=1, artifact_id=artifact, artifact_digest="a" * 64,
                   application_key="app", display_name="App", requested_by=uuid7(),
                   release_id=rel.id, release_digest=rel.digest,
                   evaluation_run_id=evaluation)  # fmt: skip
        o.commit()


@pytest.mark.parametrize(
    ("withdrawn", "plan", "kept"), [(False, True, True), (True, True, False), (False, False, False)]
)
def test_a_case_run_of_a_passed_evaluation_a_plan_uses_keeps_its_family(
    cli: CliRunner, owner_db: Database, withdrawn: bool, plan: bool, kept: bool
) -> None:
    """(f) the evaluation pin, and the same family deleted once the release is withdrawn or no
    plan names the evaluation"""
    c = _client(owner_db)
    root = _run(owner_db, c.tenant, ended_days_ago=OLD)
    case = _run(owner_db, c.tenant, ended_days_ago=OLD, root=root)
    _evaluation(owner_db, c.tenant, case, withdrawn=withdrawn, plan=plan)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    line = _line(result.output, c.slug)
    if kept:
        assert line.startswith(f"{c.slug}: {_counts(0, 2, 0)}")
        assert _present(owner_db, root) == _present(owner_db, case) == FULL
    else:
        assert line.startswith(f"{c.slug}: {_counts(2, 0, 2)}")
        assert _present(owner_db, root) == _present(owner_db, case) == GONE


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_case_run_of_an_evaluation_that_has_not_ended_keeps_its_family(
    cli: CliRunner, owner_db: Database, status: str
) -> None:
    """Issue #174, item 4 (B-3987): an evaluation stuck in `running` (or still `queued`) pins
    its case runs whatever its release and plans, so it can still be scored; an unrelated old
    family of the same client is deleted (not vacuous)."""
    c = _client(owner_db)
    root = _run(owner_db, c.tenant, ended_days_ago=OLD)
    case = _run(owner_db, c.tenant, ended_days_ago=OLD, root=root)
    other = _run(owner_db, c.tenant, ended_days_ago=OLD)
    _evaluation(owner_db, c.tenant, case, withdrawn=True, plan=False, status=status)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug).startswith(f"{c.slug}: {_counts(1, 2, 1)}")
    assert _present(owner_db, root) == _present(owner_db, case) == FULL
    assert _present(owner_db, other) == GONE


def test_a_delete_at_the_retention_floor_still_works(cli: CliRunner, owner_db: Database) -> None:
    """Issue #174 (B-3987): with `--older-than` and both run classes' stored `retain_days` at the
    floor, the command's cutoff is exactly the floor the guard enforces, and the old family goes."""
    floor = max(RETENTION_FLOORS[c] for c in RUN_CLASSES)
    c = _client(owner_db)
    for data_class in RUN_CLASSES:
        _retention(owner_db, c.tenant, data_class, floor, False)
    run = _run(owner_db, c.tenant, ended_days_ago=floor + 1)
    result = _delete(cli, "--older-than", str(floor))
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug).startswith(f"{c.slug}: {_counts(1, 0, 1)}")
    assert _present(owner_db, run) == GONE
    (event,) = _events(owner_db, c.tenant)
    assert event.details["effective_days"] == floor


# ------------------------------------------------------------------ guards (13 g)
@pytest.fixture(params=["app", "owner"])
def guard_session(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Iterator[tuple[Session, Client]]:
    """A session as `shifde_app` in the client's own context, or as the schema owner."""
    c = _client(owner_db)
    database = app_db if request.param == "app" else owner_db
    with database.session() as s:
        set_tenant(s, c.tenant)
        yield s, c
        s.rollback()


def _refused(s: Session, sql: str, cutoff_days: int | None, **params: Any) -> None:
    """`sql` in the session's open transaction (after the cutoff, if given) fails with 42501."""
    if cutoff_days is not None:
        s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=cutoff_days)).isoformat()})
    with pytest.raises(DBAPIError) as caught:
        s.execute(text(sql), params)
    assert getattr(caught.value.orig, "sqlstate", None) == "42501", caught.value
    s.rollback()


def test_guard_refuses_a_delete_without_a_cutoff(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    s, c = guard_session
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    set_tenant(s, c.tenant)
    for table in RUN_TABLES:
        column = "id" if table == "runs" else "run_id"
        _refused(s, f"DELETE FROM shifde.{table} WHERE {column} = :r", None, r=run)
        set_tenant(s, c.tenant)
    assert _present(owner_db, run) == FULL


def test_guard_refuses_a_run_that_is_not_terminal(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    s, c = guard_session
    run = _run(owner_db, c.tenant, ended_days_ago=None)
    set_tenant(s, c.tenant)
    # a cutoff past the floor and the client's retention, so the run's status is what refuses
    # (B-3987, B-3993)
    _refused(s, "DELETE FROM shifde.run_events WHERE run_id = :r", 100, r=run)
    set_tenant(s, c.tenant)
    _refused(s, "DELETE FROM shifde.runs WHERE id = :r", 100, r=run)
    assert _present(owner_db, run) == FULL


def test_guard_refuses_a_run_that_ended_after_the_cutoff(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    s, c = guard_session
    run = _run(owner_db, c.tenant, ended_days_ago=RECENT)
    set_tenant(s, c.tenant)
    _refused(s, "DELETE FROM shifde.run_artifacts WHERE run_id = :r", 100, r=run)
    assert _present(owner_db, run) == FULL


@pytest.mark.parametrize(
    ("table", "pin"),
    [("approvals", {"approval": "pending"}), ("jobs", {"job": "queued"}),
     ("budget_reservations", {"reservation": "held"})],
)  # fmt: skip
def test_guard_refuses_a_pending_approval_a_queued_job_and_a_held_reservation(
    guard_session: tuple[Session, Client], owner_db: Database, table: str, pin: dict[str, str]
) -> None:
    s, c = guard_session
    run = _run(owner_db, c.tenant, ended_days_ago=OLD, pins=pin)
    set_tenant(s, c.tenant)
    # a cutoff past the floor and the client's retention, so the pin is what refuses (B-3987,
    # B-3993)
    _refused(s, f"DELETE FROM shifde.{table} WHERE run_id = :r", 100, r=run)
    assert _present(owner_db, run) == FULL


def test_guard_admits_a_decided_approval_of_an_old_run_with_a_cutoff(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    """The control for the refusals above: the same delete with every condition met succeeds."""
    s, c = guard_session
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    s.rollback()
    set_tenant(s, c.tenant)
    s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=100)).isoformat()})
    deleted = s.execute(text("DELETE FROM shifde.approvals WHERE run_id = :r"), {"r": run})
    assert deleted.rowcount == 1  # type: ignore[attr-defined]
    s.rollback()


def test_guard_refuses_a_row_with_a_null_run_id(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    """Only `jobs` and `ontology_query_lineage` allow a NULL run_id; the other seven tables have
    it `NOT NULL` (`runs` uses its primary key). The job is done and the lineage row has no
    status, so the NULL run is the only failing condition."""
    s, c = guard_session
    job, lineage = uuid7(), uuid7()
    with owner_db.session() as o:
        _, ontology_version = add_published(o, c.tenant, "ontologies", f"onto-{uuid7().hex}", {})
        insert(o, "jobs", id=job, tenant_id=c.tenant, kind="run", run_id=None, status="done",
               dedupe_key=f"null-run-{job}")  # fmt: skip
        insert(o, "ontology_query_lineage", id=lineage, tenant_id=c.tenant, principal_id=uuid7(),
               principal_type="user", ontology_id=uuid7(), ontology_version_id=ontology_version,
               run_id=None, channel="agent", object_types=["x"], connection_ids=[],
               compiled_query_hashes=[], duration_ms=1, outcome="success")  # fmt: skip
        o.commit()
    set_tenant(s, c.tenant)
    _refused(s, "DELETE FROM shifde.jobs WHERE id = :i", 100, i=job)
    set_tenant(s, c.tenant)
    _refused(s, "DELETE FROM shifde.ontology_query_lineage WHERE id = :i", 100, i=lineage)
    set_tenant(s, c.tenant)
    with owner_db.session() as o:
        for table, row in (("jobs", job), ("ontology_query_lineage", lineage)):
            found = o.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE id = :i"), {"i": row}
            )
            assert found.scalar_one() == 1, table


# ------------------------------------------------------------------ legal hold and floor (#174)
def _migration(revision: str) -> Any:  # Any: the migration module
    script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://unused/x"))
    found = script.get_revision(revision)
    assert found is not None, revision
    return found.module


def test_the_guard_floor_is_the_floor_the_command_applies() -> None:
    """runtime_0008 mirrors the floor in SQL; it must equal what `effective_days` enforces."""
    assert max(RETENTION_FLOORS[c] for c in RUN_CLASSES) == _migration("runtime_0008").FLOOR_DAYS


def test_the_downgrade_body_is_runtime_0006s_guard() -> None:
    assert _migration("runtime_0008").PREVIOUS_GUARD == _migration("runtime_0006").GUARD


def test_the_runtime_0009_floor_and_defaults_are_the_ones_the_command_applies() -> None:
    """runtime_0009 mirrors `effective_days` in SQL; its literals must equal the command's."""
    module = _migration("runtime_0009")
    assert max(RETENTION_FLOORS[c] for c in RUN_CLASSES) == module.FLOOR_DAYS
    assert {c: RETENTION_DEFAULTS[c] for c in RUN_CLASSES} == module.RUN_DEFAULT_DAYS


def test_the_runtime_0009_downgrade_body_is_runtime_0008s_guard() -> None:
    assert _migration("runtime_0009").PREVIOUS_GUARD == _migration("runtime_0008").GUARD


@pytest.mark.parametrize("data_class", ["run_artifacts", "usage_events"])
def test_guard_refuses_every_table_of_a_client_on_legal_hold(
    guard_session: tuple[Session, Client], owner_db: Database, data_class: str
) -> None:
    """A hold on any data class (the command's rule) refuses each of the nine deletes, with a
    valid cutoff and an old, ended, unpinned run; without the hold the same delete succeeds
    (`test_guard_admits_a_client_whose_hold_was_lifted`)."""
    s, c = guard_session
    _retention(owner_db, c.tenant, data_class, 400, True)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    s.rollback()
    for table in RUN_TABLES:
        column = "id" if table == "runs" else "run_id"
        set_tenant(s, c.tenant)
        s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=100)).isoformat()})
        with pytest.raises(DBAPIError) as caught:
            s.execute(text(f"DELETE FROM shifde.{table} WHERE {column} = :r"), {"r": run})
        assert getattr(caught.value.orig, "sqlstate", None) == "42501", caught.value
        assert "legal hold" in str(caught.value.orig)
        s.rollback()
    assert _present(owner_db, run) == FULL


def test_guard_admits_a_client_whose_hold_was_lifted(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    """A stored retention row without a hold does not refuse (the hold, not the row, is checked)."""
    s, c = guard_session
    _retention(owner_db, c.tenant, "run_artifacts", 90, False)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    s.rollback()
    set_tenant(s, c.tenant)
    s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=100)).isoformat()})
    deleted = s.execute(text("DELETE FROM shifde.approvals WHERE run_id = :r"), {"r": run})
    assert deleted.rowcount == 1  # type: ignore[attr-defined]
    s.rollback()


def test_guard_refuses_a_cutoff_later_than_the_floor(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    """A cutoff a minute inside the floor (or in the future) is refused on every table, though the
    run ended long before it; a minute outside the floor the same delete succeeds."""
    floor = timedelta(days=max(RETENTION_FLOORS[c] for c in RUN_CLASSES))
    s, c = guard_session
    # the client's retention at the floor, so the floor is the bound the control tests (B-3993)
    for data_class in RUN_CLASSES:
        _retention(owner_db, c.tenant, data_class, floor.days, False)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    s.rollback()
    for cutoff in (utcnow() - floor + timedelta(minutes=1), utcnow() + timedelta(days=365)):
        for table in RUN_TABLES:
            column = "id" if table == "runs" else "run_id"
            set_tenant(s, c.tenant)
            s.execute(text(CUTOFF), {"c": cutoff.isoformat()})
            with pytest.raises(DBAPIError) as caught:
                s.execute(text(f"DELETE FROM shifde.{table} WHERE {column} = :r"), {"r": run})
            assert getattr(caught.value.orig, "sqlstate", None) == "42501", caught.value
            assert "retention floor" in str(caught.value.orig)
            s.rollback()
    assert _present(owner_db, run) == FULL
    set_tenant(s, c.tenant)
    s.execute(text(CUTOFF), {"c": (utcnow() - floor - timedelta(minutes=1)).isoformat()})
    deleted = s.execute(text("DELETE FROM shifde.approvals WHERE run_id = :r"), {"r": run})
    assert deleted.rowcount == 1  # type: ignore[attr-defined]
    s.rollback()


# ------------------------------------------------------------------ client's retention (#347)
def _cutoff_refused(s: Session, tenant: UUID, sql: str, cutoff_days: int, run: UUID) -> None:
    """`sql` with a cutoff `cutoff_days` ago fails with 42501 for the client's retention."""
    set_tenant(s, tenant)
    s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=cutoff_days)).isoformat()})
    with pytest.raises(DBAPIError) as caught:
        s.execute(text(sql), {"r": run})
    assert getattr(caught.value.orig, "sqlstate", None) == "42501", caught.value
    assert "client's retention" in str(caught.value.orig)
    s.rollback()


def _approval_deleted(s: Session, tenant: UUID, cutoff_days: int, run: UUID) -> int:
    """Rows a delete of the run's approval with a cutoff `cutoff_days` ago removes (rolled back)."""
    set_tenant(s, tenant)
    s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=cutoff_days)).isoformat()})
    deleted = s.execute(text("DELETE FROM shifde.approvals WHERE run_id = :r"), {"r": run})
    n = int(deleted.rowcount)  # type: ignore[attr-defined]
    s.rollback()
    return n


@pytest.mark.parametrize("data_class", RUN_CLASSES)
def test_guard_refuses_a_cutoff_inside_the_clients_stored_retention(
    guard_session: tuple[Session, Client], owner_db: Database, data_class: str
) -> None:
    """Issue #347 (B-3993): a stored `retain_days = 400` of either run class refuses a cutoff 200
    days ago on each of the nine tables, though the run ended 500 days ago; 401 days ago the same
    delete of the decided approval succeeds."""
    s, c = guard_session
    _retention(owner_db, c.tenant, data_class, 400, False)
    run = _run(owner_db, c.tenant, ended_days_ago=500)
    s.rollback()
    for table in RUN_TABLES:
        column = "id" if table == "runs" else "run_id"
        _cutoff_refused(s, c.tenant, f"DELETE FROM shifde.{table} WHERE {column} = :r", 200, run)
    assert _present(owner_db, run) == FULL
    assert _approval_deleted(s, c.tenant, 401, run) == 1


def test_guard_applies_the_default_retention_without_a_stored_row(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    """Issue #347: with no stored row the 90-day default applies: 89 days is refused, 91 is not."""
    s, c = guard_session
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    s.rollback()
    _cutoff_refused(s, c.tenant, "DELETE FROM shifde.approvals WHERE run_id = :r", 89, run)
    assert _present(owner_db, run) == FULL
    assert _approval_deleted(s, c.tenant, 91, run) == 1


def test_guard_a_shorter_stored_class_does_not_shorten_retention(
    guard_session: tuple[Session, Client], owner_db: Database
) -> None:
    """Issue #347: as in `effective_days`, `run_artifacts = 30` with no `run_steps_content` row
    keeps that class's 90-day default, so a cutoff 60 days ago is refused."""
    s, c = guard_session
    _retention(owner_db, c.tenant, "run_artifacts", 30, False)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    s.rollback()
    _cutoff_refused(s, c.tenant, "DELETE FROM shifde.approvals WHERE run_id = :r", 60, run)
    assert _present(owner_db, run) == FULL


def test_the_command_takes_its_cutoff_from_the_database_clock(
    app_db: Database, owner_db: Database
) -> None:
    """B-3994: the cutoff is the client transaction's `now()` minus the effective days, the clock
    the guard compares it with, not the host's."""
    c = _client(owner_db)
    _retention(owner_db, c.tenant, "run_artifacts", 120, False)
    with app_db.session() as s:
        set_tenant(s, c.tenant)
        result = _delete_pass(
            s, c.tenant, c.slug, older_than_days=30, dry_run=True, actor_label="test"
        )
        db_now = s.execute(text("SELECT now()")).scalar_one()
        s.rollback()
    assert result.effective_days == 120
    assert result.cutoff == db_now - timedelta(days=120)


_GUARD_STATE = (
    "SELECT p.prosrc, p.proconfig, p.prosecdef, "
    "has_function_privilege('public', p.oid, 'EXECUTE'), pg_get_userbyid(p.proowner), "
    "(SELECT count(*) FROM pg_trigger t WHERE t.tgfoid = p.oid) "
    "FROM pg_proc p WHERE p.oid = 'shifde.retention_delete_guard()'::regprocedure"
)


def _body(create: str) -> str:
    return create.split("$$", 2)[1]


def test_the_downgrade_restores_the_runtime_0006_guard(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    """On a scratch copy: runtime_0009 down gives runtime_0008's body and runtime_0008 down gives
    runtime_0006's, each with the same owner, settings, invoker rights, no PUBLIC execute and the
    nine triggers; up again restores runtime_0009's."""
    name = f"{DB_PREFIX}{run_id}_rtguard"
    owner = make_url(owner_url).username or "shifde_owner"
    app_role = make_url(app_url).username or "shifde_app"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as c:
            if c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first():
                _drop_database(c, name)
            _create_database(c, name, owner, app_role, f"{DB_PREFIX}{run_id}_tmpl")
        url = _with_db(owner_url, name)
        cfg = alembic_config(url)
        engine = create_engine(url, poolclass=NullPool)
        try:
            with engine.connect() as c:
                upgraded = c.execute(text(_GUARD_STATE)).one()
            assert upgraded[0] == _body(_migration("runtime_0009").GUARD)
            command.downgrade(cfg, "runtime_0008")
            with engine.connect() as c:
                previous = c.execute(text(_GUARD_STATE)).one()
                versions = set(
                    c.execute(text("SELECT version_num FROM shifde.alembic_version")).scalars()
                )
            assert "runtime_0009" not in versions
            assert previous[0] == _body(_migration("runtime_0008").GUARD)
            assert previous[1:] == upgraded[1:]
            command.downgrade(cfg, "runtime_0007")
            with engine.connect() as c:
                downgraded = c.execute(text(_GUARD_STATE)).one()
                versions = set(
                    c.execute(text("SELECT version_num FROM shifde.alembic_version")).scalars()
                )
            assert "runtime_0008" not in versions
            assert downgraded[0] == _body(_migration("runtime_0006").GUARD)
            assert downgraded[1:] == upgraded[1:]
            assert tuple(downgraded[1:]) == (
                ["search_path=pg_catalog, pg_temp"],
                False,
                False,
                owner,
                len(RUN_TABLES),
            )
            command.upgrade(cfg, "heads")
            with engine.connect() as c:
                assert c.execute(text(_GUARD_STATE)).one() == upgraded
        finally:
            engine.dispose()
    finally:
        with contextlib.suppress(Exception), admin.connect() as c:
            _drop_database(c, name)
        admin.dispose()


# ------------------------------------------------------------------ isolation (13 h)
def _seed_audit(owner_db: Database, tenant: UUID) -> None:
    with owner_db.session() as o:
        set_tenant(o, tenant)
        audit.record(o, RequestContext(request_id="seed"), action="test.seeded",
                     tenant_id=tenant, actor_label="seed")  # fmt: skip
        o.commit()


@pytest.mark.parametrize("b_policy", ["legal_hold", "longer_retention"])
def test_deleting_a_leaves_b_byte_for_byte_unchanged(
    cli: CliRunner, owner_db: Database, b_policy: str
) -> None:
    """(h) B on legal hold, or with a stored retain_days longer than its runs' age"""
    a, b = _client(owner_db), _client(owner_db)
    if b_policy == "legal_hold":
        _retention(owner_db, b.tenant, "run_artifacts", 90, True)
    else:
        _retention(owner_db, b.tenant, "run_artifacts", 400, False)
    a_runs = [_run(owner_db, a.tenant, ended_days_ago=OLD) for _ in range(2)]
    for _ in range(2):
        _run(owner_db, b.tenant, ended_days_ago=OLD)
    for c in (a, b):
        _seed_audit(owner_db, c.tenant)
    a_before, b_before = _tenant_rows(owner_db, a.tenant), _tenant_rows(owner_db, b.tenant)
    assert all(len(b_before[t]) == 2 for t in RUN_TABLES)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    b_line = _line(result.output, b.slug)
    if b_policy == "legal_hold":
        assert b_line == f"{b.slug}: skipped (legal hold)"
    else:
        assert b_line.startswith(f"{b.slug}: {_counts(0, 0, 0)}")
    assert _line(result.output, a.slug).startswith(f"{a.slug}: {_counts(2, 0, 2)}")
    assert _tenant_rows(owner_db, b.tenant) == b_before
    a_after = _tenant_rows(owner_db, a.tenant)
    assert a_after != a_before  # not vacuous
    assert all(a_after[t] == [] for t in RUN_TABLES)
    assert all(_present(owner_db, r) == GONE for r in a_runs)
    assert len(_events(owner_db, b.tenant)) == 1


def test_the_app_role_in_a_context_deletes_none_of_b_with_a_valid_cutoff(
    app_db: Database, owner_db: Database
) -> None:
    """As `shifde_app` in A's context with a valid cutoff, a delete of B's old run reaches no row
    of B in any of the nine tables; the same deletes of A's own old run do (not vacuous)."""
    a, b = _client(owner_db), _client(owner_db)
    a_run = _run(owner_db, a.tenant, ended_days_ago=OLD)
    b_run = _run(owner_db, b.tenant, ended_days_ago=OLD)
    b_before = _tenant_rows(owner_db, b.tenant)
    with app_db.session() as s:
        set_tenant(s, a.tenant)
        s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=100)).isoformat()})
        for table in RUN_TABLES:
            column = "id" if table == "runs" else "run_id"
            deleted = s.execute(
                text(f"DELETE FROM shifde.{table} WHERE {column} = :r"), {"r": b_run}
            )
            assert deleted.rowcount == 0, table  # type: ignore[attr-defined]
        s.rollback()
        assert _tenant_rows(owner_db, b.tenant) == b_before
        assert _present(owner_db, b_run) == FULL
        set_tenant(s, a.tenant)
        s.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=100)).isoformat()})
        for table in RUN_TABLES:
            column = "id" if table == "runs" else "run_id"
            deleted = s.execute(
                text(f"DELETE FROM shifde.{table} WHERE {column} = :r"), {"r": a_run}
            )
            assert deleted.rowcount == 1, table  # type: ignore[attr-defined]
        s.rollback()
    assert _present(owner_db, a_run) == FULL


# ------------------------------------------------------------------ audit, reruns, refusals
def test_one_audit_event_per_client_and_the_chains_verify(
    cli: CliRunner, owner_db: Database
) -> None:
    """(i)"""
    a, b = _client(owner_db), _client(owner_db)
    _retention(owner_db, a.tenant, "audit_events", 365, True)
    _run(owner_db, b.tenant, ended_days_ago=OLD)
    result = _delete(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    (a_event,) = _events(owner_db, a.tenant)
    (b_event,) = _events(owner_db, b.tenant)
    for event, hold, n in ((a_event, True, 0), (b_event, False, 1)):
        assert (event.action, event.outcome) == (ACTION, "success")
        details = event.details
        assert details["older_than_days"] == 30
        assert details["effective_days"] == 90  # the run_artifacts default
        assert details["legal_hold"] is hold
        assert (details["runs"], details["runs_kept"]) == (n, 0)
        assert all(details[t] == n for t in DELETE_TABLES)
        assert details["actor"].startswith("shifde-admin:")
        assert details["cutoff"] in result.output or hold
    for slug in (a.slug, b.slug):
        verified = cli.invoke(cli_main.app, ["audit", "verify", "--org", slug])
        assert verified.exit_code == 0, verified.output


def test_a_second_run_reports_zero(cli: CliRunner, owner_db: Database) -> None:
    """(j)"""
    c = _client(owner_db)
    _run(owner_db, c.tenant, ended_days_ago=OLD)
    first = _delete(cli, "--older-than", "30")
    assert first.exit_code == 0, first.output
    assert _line(first.output, c.slug).startswith(f"{c.slug}: {_counts(1, 0, 1)}")
    second = _delete(cli, "--older-than", "30")
    assert second.exit_code == 0, second.output
    assert _line(second.output, c.slug).startswith(f"{c.slug}: {_counts(0, 0, 0)}")


@pytest.mark.parametrize("args", [["--older-than", "0"], ["--older-than", "-5"],
                                  ["--older-than", "abc"], [], ["--older-than", "36501"]])  # fmt: skip
def test_an_invalid_older_than_is_refused(
    cli: CliRunner, owner_db: Database, args: list[str]
) -> None:
    """(k)"""
    c = _client(owner_db)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    result = _delete(cli, *args)
    assert result.exit_code != 0
    assert _present(owner_db, run) == FULL
    assert _events(owner_db, c.tenant) == []


def test_a_privileged_role_is_refused(
    settings: Settings, owner_url: str, monkeypatch: pytest.MonkeyPatch, owner_db: Database
) -> None:
    """(l) The command connects as the application role: given the owner's URL it refuses."""
    c = _client(owner_db)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    owner_settings = settings.model_copy(update={"database_url": owner_url})
    monkeypatch.setattr(cli_main, "_settings", lambda: owner_settings)
    result = _delete(CliRunner(), "--older-than", "30")
    assert result.exit_code == 1
    assert "refusing to delete" in result.output
    assert _present(owner_db, run) == FULL
