"""`shifde-admin maintenance purge` (Findings Item 20, REQ F-11, B-2600): the stored content of
old, ended runs is scrubbed client by client as the application role, with the two scrubs
migration `runtime_0002` permits (artifact content -> `{"scrubbed": true}`, size and hash kept;
tool-call arguments -> NULL).

Behaviour: `--dry-run` reports the counts and changes nothing; running runs, recently ended runs
and an ended child of a running root are untouched; a stored `retain_days` longer than
`--older-than` wins; a client on legal hold is skipped; a real run writes one audit event per
client and every chain still verifies; a second run finds nothing left; invalid `--older-than`
values are refused.

Isolation: with A on legal hold and B purged, every A artifact and argument is byte-for-byte
unchanged, and B really changed (the check is not vacuous).

Data is seeded and read as the schema owner; the command itself connects as `shifde_app`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from shifde.cli import main as cli_main
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from tests._support.runs import add_run
from tests._support.tenants import add_tenant
from tests.build.env_support import add_published
from tests.runtime.conftest import insert

SCRUBBED = {"scrubbed": True}
ACTION = "maintenance.retention.purged"
OLD, RECENT = 200, 1  # days since the run ended


@dataclass(frozen=True)
class Client:
    tenant: UUID
    slug: str


@pytest.fixture
def cli(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(cli_main, "_settings", lambda: settings)
    return CliRunner()


def _purge(runner: CliRunner, *args: str) -> Any:  # Any: click Result
    return runner.invoke(cli_main.app, ["maintenance", "purge", *args])


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


def _content(o: Session, tenant: UUID, run: UUID) -> None:
    """An input artifact, a step with an intermediate artifact and a tool call with arguments."""
    _, tool_version = add_published(o, tenant, "tools", f"tool-{uuid7().hex}", {})
    repo.insert_artifact(o, tenant, run, "input", {"input": f"in-{run.hex}"})
    step = uuid7()
    insert(o, "run_steps", id=step, tenant_id=tenant, run_id=run, seq=1, kind="model_call",
           name="m", status="succeeded")  # fmt: skip
    repo.insert_artifact(o, tenant, run, "intermediate", {"text": f"model-{run.hex}"}, step_id=step)
    invocation = uuid7()
    insert(o, "tool_invocations", id=invocation, tenant_id=tenant, run_id=run, call_ref="w1",
           tool_version_id=tool_version, side_effect_class="write_non_idempotent",
           idempotency_key=f"k-{invocation}", status="succeeded", request_hash="h",
           arguments=f'{{"item": "arg-{run.hex}"}}')  # fmt: skip


def _run(
    owner_db: Database, tenant: UUID, *, ended_days_ago: int | None, root: UUID | None = None
) -> UUID:
    """A run of `tenant` with content: running when `ended_days_ago` is None, else cancelled that
    many days ago. With `root` it is a child of that run."""
    tree = {} if root is None else {"root_run_id": root, "parent_run_id": root, "depth": 1}
    with owner_db.session() as o:
        if ended_days_ago is None:
            run = add_run(o, tenant, status="running", lease_token=uuid7(),
                          started_at=utcnow(), **tree)  # fmt: skip
            _content(o, tenant, run)
        else:
            run = add_run(o, tenant, status="pending", **tree)
            _content(o, tenant, run)
            o.execute(
                text(
                    "UPDATE shifde.runs SET status = 'cancelled', "
                    "state_version = state_version + 1, finished_at = :f WHERE id = :r"
                ),
                {"f": utcnow() - timedelta(days=ended_days_ago), "r": run},
            )
        o.commit()
    return run


def _snapshot(owner_db: Database, run: UUID) -> list[tuple[str, ...]]:
    """Every artifact (content as text, size, hash) and invocation (arguments as text) of `run`."""
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT 'a', id::text, kind, content::text, size_bytes::text, sha256 "
                "FROM shifde.run_artifacts WHERE run_id = :r "
                "UNION ALL SELECT 'i', id::text, '', COALESCE(arguments::text, 'NULL'), '', '' "
                "FROM shifde.tool_invocations WHERE run_id = :r ORDER BY 1, 2"
            ),
            {"r": run},
        ).all()
    return [tuple(r) for r in rows]


def _scrubbed(owner_db: Database, run: UUID) -> bool:
    with owner_db.session() as o:
        contents = o.execute(
            text("SELECT content FROM shifde.run_artifacts WHERE run_id = :r"), {"r": run}
        ).scalars()
        arguments = o.execute(
            text("SELECT arguments FROM shifde.tool_invocations WHERE run_id = :r"), {"r": run}
        ).scalars()
        return all(c == SCRUBBED for c in contents) and all(a is None for a in arguments)


def _events(owner_db: Database, tenant: UUID) -> list[Any]:  # Any: audit rows
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT action, outcome, details FROM shifde.audit_events "
                    "WHERE tenant_id = :t ORDER BY chain_seq"
                ),
                {"t": tenant},
            ).all()
        )


def _line(output: str, slug: str) -> str:
    lines = [ln for ln in output.splitlines() if ln.startswith(f"{slug}:")]
    assert len(lines) == 1, output
    return lines[0]


def test_dry_run_reports_counts_and_changes_nothing(cli: CliRunner, owner_db: Database) -> None:
    """(a)"""
    c = _client(owner_db)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    before = _snapshot(owner_db, run)
    result = _purge(cli, "--older-than", "30", "--dry-run")
    assert result.exit_code == 0, result.output
    line = _line(result.output, c.slug)
    assert line.startswith(f"{c.slug}: runs 1, artifacts 2, tool invocations 1 (cutoff ")
    assert result.output.rstrip().endswith("dry run: nothing changed")
    assert _snapshot(owner_db, run) == before
    assert _events(owner_db, c.tenant) == []
    # a real run right after reports the same counts
    real = _purge(cli, "--older-than", "30")
    assert real.exit_code == 0, real.output
    assert _line(real.output, c.slug).split(" (cutoff")[0] == line.split(" (cutoff")[0]


def test_real_purge_scrubs_an_old_ended_run_and_keeps_size_and_hash(
    cli: CliRunner, owner_db: Database
) -> None:
    """(b)"""
    c = _client(owner_db)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    before = _snapshot(owner_db, run)
    assert {r[2] for r in before if r[0] == "a"} == {"input", "intermediate"}
    result = _purge(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert "dry run" not in result.output
    assert _line(result.output, c.slug).startswith(
        f"{c.slug}: runs 1, artifacts 2, tool invocations 1 (cutoff "
    )
    after = _snapshot(owner_db, run)
    assert _scrubbed(owner_db, run)
    for old, new in zip(before, after, strict=True):
        assert old[:3] == new[:3]  # same row, same kind
        assert old[4:] == new[4:]  # size and hash kept
        assert old[3] != new[3]


def test_running_recent_and_child_of_running_root_are_untouched(
    cli: CliRunner, owner_db: Database
) -> None:
    """(c)"""
    c = _client(owner_db)
    running = _run(owner_db, c.tenant, ended_days_ago=None)
    recent = _run(owner_db, c.tenant, ended_days_ago=RECENT)
    child = _run(owner_db, c.tenant, ended_days_ago=OLD, root=running)
    old = _run(owner_db, c.tenant, ended_days_ago=OLD)
    kept = {r: _snapshot(owner_db, r) for r in (running, recent, child)}
    result = _purge(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug).startswith(f"{c.slug}: runs 1, ")
    assert {r: _snapshot(owner_db, r) for r in kept} == kept
    assert _scrubbed(owner_db, old)


def test_stored_retention_longer_than_older_than_keeps_the_run(
    cli: CliRunner, owner_db: Database
) -> None:
    """(d)"""
    c = _client(owner_db)
    _retention(owner_db, c.tenant, "run_artifacts", 400, False)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    before = _snapshot(owner_db, run)
    result = _purge(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug).startswith(
        f"{c.slug}: runs 0, artifacts 0, tool invocations 0 (cutoff "
    )
    assert _snapshot(owner_db, run) == before
    (event,) = _events(owner_db, c.tenant)
    assert event.details["effective_days"] == 400


def test_a_client_on_legal_hold_is_skipped(cli: CliRunner, owner_db: Database) -> None:
    """(e)"""
    c = _client(owner_db)
    _retention(owner_db, c.tenant, "usage_events", 395, True)  # a hold on any data class
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    before = _snapshot(owner_db, run)
    result = _purge(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, c.slug) == f"{c.slug}: skipped (legal hold)"
    assert _snapshot(owner_db, run) == before
    (event,) = _events(owner_db, c.tenant)
    assert event.action == ACTION
    # `audit_events.outcome` allows only success / denied / failure (audit_0001): the skip is
    # recorded as `legal_hold: true` with zero counts (build.md, Blocked)
    assert event.details["legal_hold"] is True
    assert (event.details["runs"], event.details["artifacts"]) == (0, 0)


def test_purging_b_leaves_a_on_legal_hold_byte_for_byte_unchanged(
    cli: CliRunner, owner_db: Database
) -> None:
    """(f)"""
    a, b = _client(owner_db), _client(owner_db)
    _retention(owner_db, a.tenant, "run_artifacts", 90, True)
    a_runs = [_run(owner_db, a.tenant, ended_days_ago=OLD) for _ in range(2)]
    b_runs = [_run(owner_db, b.tenant, ended_days_ago=OLD) for _ in range(2)]
    a_before = {r: _snapshot(owner_db, r) for r in a_runs}
    b_before = {r: _snapshot(owner_db, r) for r in b_runs}
    result = _purge(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    assert _line(result.output, a.slug) == f"{a.slug}: skipped (legal hold)"
    assert _line(result.output, b.slug).startswith(f"{b.slug}: runs 2, artifacts 4, ")
    assert {r: _snapshot(owner_db, r) for r in a_runs} == a_before
    assert {r: _snapshot(owner_db, r) for r in b_runs} != b_before  # not vacuous
    assert all(_scrubbed(owner_db, r) for r in b_runs)


def test_one_audit_event_per_client_and_the_chains_verify(
    cli: CliRunner, owner_db: Database
) -> None:
    """(g)"""
    a, b = _client(owner_db), _client(owner_db)
    _retention(owner_db, a.tenant, "audit_events", 365, True)
    _run(owner_db, b.tenant, ended_days_ago=OLD)
    result = _purge(cli, "--older-than", "30")
    assert result.exit_code == 0, result.output
    (a_event,) = _events(owner_db, a.tenant)
    (b_event,) = _events(owner_db, b.tenant)
    for event, hold, counts in ((a_event, True, (0, 0, 0)), (b_event, False, (1, 2, 1))):
        assert (event.action, event.outcome) == (ACTION, "success")
        details = event.details
        assert details["older_than_days"] == 30
        assert details["effective_days"] == 90  # the run_artifacts default
        assert details["legal_hold"] is hold
        assert (details["runs"], details["artifacts"], details["tool_invocations"]) == counts
        assert details["actor"].startswith("shifde-admin:")
        assert details["cutoff"] in result.output or hold
    for slug in (a.slug, b.slug):
        verified = cli.invoke(cli_main.app, ["audit", "verify", "--org", slug])
        assert verified.exit_code == 0, verified.output


@pytest.mark.parametrize("args", [["--older-than", "0"], ["--older-than", "-5"],
                                  ["--older-than", "abc"], [], ["--older-than", "36501"]])  # fmt: skip
def test_an_invalid_older_than_is_refused(
    cli: CliRunner, owner_db: Database, args: list[str]
) -> None:
    """(h)"""
    c = _client(owner_db)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    before = _snapshot(owner_db, run)
    result = _purge(cli, *args)
    assert result.exit_code != 0
    assert _snapshot(owner_db, run) == before
    assert _events(owner_db, c.tenant) == []


def test_a_second_run_reports_zero(cli: CliRunner, owner_db: Database) -> None:
    """(i)"""
    c = _client(owner_db)
    _run(owner_db, c.tenant, ended_days_ago=OLD)
    first = _purge(cli, "--older-than", "30")
    assert first.exit_code == 0, first.output
    assert _line(first.output, c.slug).startswith(f"{c.slug}: runs 1, ")
    second = _purge(cli, "--older-than", "30")
    assert second.exit_code == 0, second.output
    assert _line(second.output, c.slug).startswith(
        f"{c.slug}: runs 0, artifacts 0, tool invocations 0 (cutoff "
    )


def test_a_privileged_role_is_refused(
    settings: Settings, owner_url: str, monkeypatch: pytest.MonkeyPatch, owner_db: Database
) -> None:
    """The command connects as the application role: given the owner's URL it refuses."""
    c = _client(owner_db)
    run = _run(owner_db, c.tenant, ended_days_ago=OLD)
    before = _snapshot(owner_db, run)
    owner_settings = settings.model_copy(update={"database_url": owner_url})
    monkeypatch.setattr(cli_main, "_settings", lambda: owner_settings)
    result = _purge(CliRunner(), "--older-than", "30")
    assert result.exit_code == 1
    assert "refusing to purge" in result.output
    assert _snapshot(owner_db, run) == before
