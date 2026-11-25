"""The two retention delete guards refuse a session of another client (issue #364, B-4089):
`shifde.retention_delete_guard()` (runs, `runtime_0010`) and
`shifde.assist_retention_delete_guard()` (assist requests, `assist_0003`).

A BEFORE DELETE row trigger runs after the row passed row-level security and the statement's
WHERE, so a DELETE whose WHERE (or a CTE it reads) calls `set_config('app.tenant_id', ...)` reaches
the guard with the row of one client and the session of another, or of none. The guard then reads
no retention policy of the row's client: `assist_0002` admitted such a delete under a legal hold
(`runtime_0009` refused it, because its read of `shifde.runs` found no run either).
Both guards now compare `shifde.current_tenant_id()` with `OLD.tenant_id` before they read a
policy.

As `shifde_app`, on every guarded table (pinned to the catalog's triggers): a switch to another
client or to none, in the WHERE or through a CTE, is refused with 42501 and the guard's own
message and deletes nothing, legal hold or not; a session of another client (or of none) deletes
nothing; the same delete in the row's client succeeds and so does the retention delete of a whole
family. The schema owner bypasses row-level security, so for it the guard is what refuses the
wrong session. A cascade meets the guard with the session as the statement left it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.cli.maintenance import DELETE_TABLES, _delete_pass
from shifde.core.clock import utcnow
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config
from tests._support.tenants import add_tenant
from tests.cli.test_maintenance_delete import (
    CUTOFF,
    FULL,
    GONE,
    _client,
    _present,
    _retention,
    _run,
)
from tests.runtime.conftest import insert

OLD = 200  # days since the run ended and the assist rows were created
RUN_TABLES = (*DELETE_TABLES, "runs")
ASSIST_TABLES = ("assist_proposals", "assist_requests")
MESSAGE = "only in a session of that client"
TABLES = pytest.mark.parametrize("table", (*RUN_TABLES, *ASSIST_TABLES))

# (the CTE, what the delete's WHERE is ANDed with): each switches the client once the row has passed
# row-level security. The reference to the row is what makes it so: a condition that reads no column
# cannot leak a row, so the planner may evaluate it before the policy, which then hides the row
# (`test_a_switch_that_reads_no_column_deletes_nothing`).
ATTACKS = {
    "where": (
        "",
        "set_config('app.tenant_id', CASE WHEN x.tenant_id IS NOT NULL THEN CAST(:to AS text) END, "
        "true) IS NOT NULL",
    ),
    "cte": (
        "WITH s AS MATERIALIZED (SELECT set_config('app.tenant_id', :to, true) AS v) ",
        "(SELECT s.v FROM s WHERE x.tenant_id IS NOT NULL) IS NOT NULL",
    ),
}
ATTACK = pytest.mark.parametrize("attack", sorted(ATTACKS))


@dataclass(frozen=True)
class Seeded:
    tenant: UUID
    slug: str
    other: UUID
    run: UUID
    request: UUID

    def where(self, table: str) -> tuple[str, UUID]:
        """The column and value that name the client's one row of `table`."""
        if table == "assist_requests":
            return "id", self.request
        if table == "assist_proposals":
            return "request_id", self.request
        return ("id" if table == "runs" else "run_id"), self.run


@pytest.fixture
def seeded(owner_db: Database) -> Seeded:
    """A client with an old, ended, unpinned run (a row in each run table) and an old refused
    assist request with a proposal, and another client."""
    client = _client(owner_db)
    run = _run(owner_db, client.tenant, ended_days_ago=OLD)
    request, user = uuid7(), uuid7()
    created = utcnow() - timedelta(days=OLD)
    with owner_db.session() as o:
        other = add_tenant(o)
        ws, project = o.execute(
            text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": run}
        ).one()
        insert(o, "users", id=user, primary_email=f"u-{user.hex}@guard.local", display_name="u")
        insert(o, "assist_requests", tenant_id=client.tenant, id=request, workspace_id=ws,
               project_id=project, requested_by=user, text="x", status="refused",
               created_at=created)  # fmt: skip
        # its run is not a run, so the retention delete removes the request and the proposal
        insert(o, "assist_proposals", tenant_id=client.tenant, id=uuid7(), request_id=request,
               run_id=uuid7(), step_key="k", kind="evaluation_run", payload="{}",
               created_at=created)  # fmt: skip
        o.commit()
    return Seeded(client.tenant, client.slug, other, run, request)


def _left(owner_db: Database, s: Seeded, table: str) -> int:
    column, value = s.where(table)
    with owner_db.session() as o:
        n = o.execute(
            text(f"SELECT count(*) FROM shifde.{table} WHERE {column} = :v"),  # nosec B608
            {"v": value},
        ).scalar_one()
    return int(n)


def _enter(session: Session, tenant: UUID | None) -> None:
    """A new transaction in `tenant`'s context with a cutoff the guards admit for the rows."""
    session.rollback()
    set_tenant(session, tenant)
    session.execute(text(CUTOFF), {"c": (utcnow() - timedelta(days=100)).isoformat()})


def _delete(
    session: Session,
    s: Seeded,
    table: str,
    attack: str | None = None,
    to: str = "",
    tail: str = "",
) -> int:
    column, value = s.where(table)
    cte, extra = ATTACKS[attack] if attack else ("", "true")
    sql = f"{cte}DELETE FROM shifde.{table} x WHERE x.{column} = :v AND {extra} {tail}"  # nosec B608
    params = {"v": value, "to": to} if ":to" in sql else {"v": value}
    return int(session.execute(text(sql), params).rowcount)  # type: ignore[attr-defined]


def _refused(
    session: Session,
    s: Seeded,
    table: str,
    attack: str | None = None,
    to: str = "",
    tail: str = "",
) -> None:
    with pytest.raises(DBAPIError) as caught:
        _delete(session, s, table, attack, to, tail)
    assert getattr(caught.value.orig, "sqlstate", None) == "42501", caught.value
    assert MESSAGE in str(caught.value.orig), (table, caught.value.orig)
    session.rollback()


def test_these_are_the_tables_the_guards_protect(owner_engine: Engine) -> None:
    with owner_engine.connect() as c:
        found = c.execute(
            text(
                "SELECT c.relname, p.proname, a.attnotnull FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_proc p ON p.oid = t.tgfoid "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
                "WHERE p.proname IN ('retention_delete_guard', 'assist_retention_delete_guard')"
            )
        ).all()
    # tenant_id is NOT NULL on each, so a session without a client (NULL) is always distinct
    assert {tuple(r) for r in found} == {
        *((t, "retention_delete_guard", True) for t in RUN_TABLES),
        *((t, "assist_retention_delete_guard", True) for t in ASSIST_TABLES),
    }


@TABLES
@ATTACK
@pytest.mark.parametrize("hold", [False, True], ids=["no_hold", "legal_hold"])
def test_a_delete_that_switches_the_client_is_refused(
    seeded: Seeded, owner_db: Database, app_db: Database, table: str, attack: str, hold: bool
) -> None:
    """As `shifde_app` in the row's own client: switching to another client or to none inside the
    statement is refused by the guard and deletes nothing, also under the client's legal hold."""
    if hold:
        _retention(owner_db, seeded.tenant, "run_artifacts", 400, True)
    with app_db.session() as s:
        for to in (str(seeded.other), ""):
            _enter(s, seeded.tenant)
            _refused(s, seeded, table, attack, to)
            assert _left(owner_db, seeded, table) == 1
    assert _present(owner_db, seeded.run) == FULL


@TABLES
def test_a_switch_that_reads_no_column_deletes_nothing(
    seeded: Seeded, owner_db: Database, app_db: Database, table: str
) -> None:
    """The plan decides whether the policy (0 rows) or the guard (42501) meets the switch."""
    column, value = seeded.where(table)
    sql = (
        f"DELETE FROM shifde.{table} WHERE {column} = :v "  # nosec B608
        "AND set_config('app.tenant_id', :to, true) IS NOT NULL RETURNING 1"
    )
    with app_db.session() as s:
        for to in (str(seeded.other), ""):
            _enter(s, seeded.tenant)
            try:
                assert s.execute(text(sql), {"v": value, "to": to}).all() == []
                s.commit()
            except DBAPIError as exc:
                assert getattr(exc.orig, "sqlstate", None) == "42501", exc
                assert MESSAGE in str(exc.orig), exc
                s.rollback()
            assert _left(owner_db, seeded, table) == 1


@TABLES
def test_a_session_of_another_client_deletes_nothing(
    seeded: Seeded, owner_db: Database, app_db: Database, table: str
) -> None:
    """As `shifde_app` row-level security hides the row; for the schema owner, who bypasses it, the
    guard refuses."""
    for tenant in (seeded.other, None):
        with app_db.session() as s:
            _enter(s, tenant)
            assert _delete(s, seeded, table) == 0
            s.commit()
        assert _left(owner_db, seeded, table) == 1
        with owner_db.session() as o:
            _enter(o, tenant)
            _refused(o, seeded, table)
        assert _left(owner_db, seeded, table) == 1


@TABLES
@pytest.mark.parametrize("role", ["app", "owner"])
def test_the_rows_own_client_still_passes_the_client_check(
    seeded: Seeded, owner_db: Database, app_db: Database, table: str, role: str
) -> None:
    """The control: the statement of the refusals above, in the row's client and without a switch
    or switching to the same client, passes the guard. A row that other rows reference is then
    refused by the foreign key (23503), which is checked after the guard admitted it."""
    referenced = ("runs", "tool_invocations")
    with (app_db if role == "app" else owner_db).session() as s:
        for attack in (None, *sorted(ATTACKS)):
            _enter(s, seeded.tenant)
            if table in referenced:
                with pytest.raises(DBAPIError) as caught:
                    _delete(s, seeded, table, attack, str(seeded.tenant))
                assert getattr(caught.value.orig, "sqlstate", None) == "23503", caught.value
            else:
                assert _delete(s, seeded, table, attack, str(seeded.tenant)) == 1
            s.rollback()


def test_a_cascade_meets_the_guard_with_the_session_the_statement_left(
    seeded: Seeded, owner_db: Database, app_db: Database
) -> None:
    """A request deleted in its own client cascades to its proposal; switching the client after
    the request's guard passed (in RETURNING) has the proposal's guard refuse the statement."""
    with app_db.session() as s:
        for to in (str(seeded.other), ""):
            _enter(s, seeded.tenant)
            _refused(s, seeded, "assist_requests", None, to,
                     "RETURNING set_config('app.tenant_id', :to, true)")  # fmt: skip
            for table in ASSIST_TABLES:
                assert _left(owner_db, seeded, table) == 1
        _enter(s, seeded.tenant)
        assert _delete(s, seeded, "assist_requests") == 1
        s.commit()
    for table in ASSIST_TABLES:
        assert _left(owner_db, seeded, table) == 0


def test_the_retention_delete_still_works(
    seeded: Seeded, owner_db: Database, app_db: Database
) -> None:
    """`maintenance delete`'s client pass as `shifde_app`: the run family, the request and the
    proposal go, through the same guards."""
    with app_db.session() as s:
        set_tenant(s, seeded.tenant)
        result = _delete_pass(
            s, seeded.tenant, seeded.slug, older_than_days=100, dry_run=False, actor_label="test"
        )
        s.commit()
    assert (result.runs, result.assist_requests, result.assist_proposals) == (1, 1, 1)
    assert _present(owner_db, seeded.run) == GONE
    for table in ASSIST_TABLES:
        assert _left(owner_db, seeded, table) == 0


# ------------------------------------------------------------------ the migrations
def _migration(revision: str) -> Any:  # Any: the migration module
    script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://unused/x"))
    found = script.get_revision(revision)
    assert found is not None, revision
    return found.module


CHECK = """  -- NULL (a session of no client) is distinct from every tenant_id, so it is refused
  IF shifde.current_tenant_id() IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'the rows of a client can be deleted only in a session of that client'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
"""


@pytest.mark.parametrize(
    ("revision", "previous"), [("runtime_0010", "runtime_0009"), ("assist_0003", "assist_0002")]
)
def test_the_new_body_adds_only_the_client_check(revision: str, previous: str) -> None:
    """Besides the check, which stands before the floor check and so before any read of a policy,
    nothing in the function changes; the downgrade body is the previous revision's."""
    new, old = _migration(revision), _migration(previous)
    assert new.down_revision == previous
    assert old.GUARD == new.PREVIOUS_GUARD
    assert new.GUARD.count(CHECK) == 1
    assert new.GUARD.replace(CHECK, "") == old.GUARD
    assert new.GUARD.index(CHECK) < new.GUARD.index("shifde.retention_policies")
    assert new.GUARD.index(CHECK + "  IF v_cutoff > pg_catalog.now()") > 0
