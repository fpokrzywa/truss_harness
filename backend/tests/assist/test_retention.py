"""SP7-4 (Call S7-J, design section 8): `maintenance purge` and `delete` cover assist requests.

Purge blanks the text and context of a request older than the cutoff that is routed, refused or
failed and whose run is gone or has ended; a newer request and one whose run still runs keep
theirs. Delete, after the run families, removes such a request whose run is gone (the run the
same pass deleted included) and its proposals, and keeps one whose run still exists. `--dry-run`
counts and changes nothing; a client on legal hold is skipped; each client's pass counts and
touches only its own rows; the counts are in the audit details. Both run as `shifde_app` on a
database of the test's own (the full-app harness), rows seeded as the schema owner.

Guard (`assist_0002`, issue #351, B-4010): for clients A and B, a direct DELETE of an ended request
and of a proposal as `shifde_app` is refused with 42501 under the client's legal hold (any data
class), with a cutoff inside the retention floor and with a cutoff inside the client's run
retention (stored, of either run class, or the 90-day default); the same delete succeeds once only
that condition is lifted, and the other client's hold or longer retention changes nothing. In the
other client's context a delete with a valid cutoff removes 0 rows; in the owning client's it
succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: row values
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.cli import maintenance
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.runs import add_run
from tests.assist.helpers import project_of, rows, user_of
from tests.conftest import CLEAN_TABLES
from tests.tenancy.conftest import app_session

for _table in ("retention_policies", "runs"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)

OLD = "200 days"
CONTEXT = '{"kind": "solution", "id": "01900000-0000-7000-8000-00000000abcd"}'


@dataclass(frozen=True)
class Seeded:
    old_ended: str  # refused long ago, no run
    recent: str  # refused now
    running: str  # routed long ago, its run still running
    run_ended: str  # routed long ago, its run ended recently (kept by delete)
    run_deleted: str  # routed long ago, its run ended long ago (the delete pass deletes it)
    proposal: str  # of `run_deleted`, created long ago
    b_old_ended: str  # client B's refused request of long ago


def _run(owner: Session, slug: str, ended: str | None) -> UUID:
    """A run of `slug`'s P1: running (`ended` None), else cancelled `ended` ago."""
    ws, proj = project_of(owner, slug)
    tenant = tenant_id(owner, slug)
    if ended is None:
        running = add_run(owner, tenant, workspace_id=UUID(ws), project_id=UUID(proj),
                          status="running", lease_token=uuid7(), started_at=utcnow())  # fmt: skip
        owner.commit()  # the helpers' reads roll the session back
        return running
    run = add_run(owner, tenant, workspace_id=UUID(ws), project_id=UUID(proj), status="pending")
    owner.execute(
        text("UPDATE shifde.runs SET status = 'cancelled', state_version = state_version + 1, "
             "finished_at = now() - CAST(:e AS interval) WHERE id = :r"),
        {"e": ended, "r": run},
    )  # fmt: skip
    owner.commit()  # the helpers' reads roll the session back
    return run


def _request(owner: Session, slug: str, status: str, age: str, run_id: UUID | None = None) -> str:
    ws, proj = project_of(owner, slug)
    request_id = uuid7()
    owner.execute(
        text("INSERT INTO shifde.assist_requests (tenant_id, id, workspace_id, project_id, "
             "requested_by, text, context, status, specialist_key, run_id, created_at) VALUES "
             "(:t, :id, :w, :p, :u, 'how much did we spend', CAST(:c AS jsonb), :s, 'cost', :run, "
             "now() - CAST(:age AS interval))"),
        {"t": tenant_id(owner, slug), "id": request_id, "w": ws, "p": proj, "c": CONTEXT,
         "u": user_of(owner, "admin@demo-a.local"), "s": status, "run": run_id, "age": age},
    )  # fmt: skip
    owner.commit()  # the helpers' reads roll the session back
    return str(request_id)


@pytest.fixture
def seeded(world: World, owner_session: Session) -> Seeded:
    o = owner_session
    o.rollback()
    run_deleted = _run(o, "demo-a", OLD)
    out = Seeded(
        old_ended=_request(o, "demo-a", "refused", OLD),
        recent=_request(o, "demo-a", "refused", "0 seconds"),
        running=_request(o, "demo-a", "routed", OLD, _run(o, "demo-a", None)),
        run_ended=_request(o, "demo-a", "routed", OLD, _run(o, "demo-a", "1 hour")),
        run_deleted=_request(o, "demo-a", "routed", OLD, run_deleted),
        proposal=str(uuid7()),
        b_old_ended=_request(o, "demo-b", "failed", OLD),
    )
    o.execute(
        text("INSERT INTO shifde.assist_proposals (tenant_id, id, request_id, run_id, step_key, "
             "kind, payload, created_at) VALUES (:t, :id, :r, :run, 'k', 'evaluation_run', "
             "'{}'::jsonb, now() - CAST(:age AS interval))"),
        {"t": tenant_id(o, "demo-a"), "id": out.proposal, "r": out.run_deleted,
         "run": run_deleted, "age": OLD},
    )  # fmt: skip
    o.commit()
    return out


def _texts(owner: Session) -> dict[str, tuple[str, Any]]:
    return {r[0]: (r[1], r[2]) for r in rows(
        owner, "SELECT id::text, text, context FROM shifde.assist_requests")}  # fmt: skip


def _hold(owner: Session, slug: str) -> None:
    owner.rollback()
    owner.execute(
        text("INSERT INTO shifde.retention_policies (tenant_id, data_class, retain_days, "
             "legal_hold) VALUES (:t, 'run_artifacts', 90, true)"),
        {"t": tenant_id(owner, slug)},
    )  # fmt: skip
    owner.commit()


def _by_slug(results: list[Any]) -> dict[str, Any]:  # Any: ClientPurge / ClientDelete
    return {r.slug: r for r in results}


def _events(owner: Session, slug: str, action: str) -> list[dict[str, Any]]:
    return [r[0] for r in rows(
        owner, "SELECT details FROM shifde.audit_events WHERE tenant_id = :t AND action = :a",
        t=tenant_id(owner, slug), a=action)]  # fmt: skip


def _purge(app_db: Database, dry_run: bool) -> dict[str, Any]:
    return _by_slug(list(maintenance.purge(app_db, older_than_days=30, now=utcnow(),
                                           dry_run=dry_run, actor_label="test")))  # fmt: skip


def _delete(app_db: Database, dry_run: bool) -> dict[str, Any]:
    return _by_slug(list(maintenance.delete(app_db, older_than_days=30,
                                            dry_run=dry_run, actor_label="test")))  # fmt: skip


def test_purge_blanks_old_ended_requests_only(
    seeded: Seeded, owner_session: Session, app_db: Database
) -> None:
    _hold(owner_session, "demo-b")
    before = _texts(owner_session)
    dry = _purge(app_db, dry_run=True)
    assert dry["demo-a"].assist_requests == 3  # old_ended, run_ended, run_deleted
    assert _texts(owner_session) == before
    assert _events(owner_session, "demo-a", maintenance.ACTION) == []
    done = _purge(app_db, dry_run=False)
    assert done["demo-a"].assist_requests == 3
    assert done["demo-b"].legal_hold and done["demo-b"].assist_requests == 0
    after = _texts(owner_session)
    for request in (seeded.old_ended, seeded.run_ended, seeded.run_deleted):
        assert after[request] == ("", None)
    for request in (seeded.recent, seeded.running, seeded.b_old_ended):
        assert after[request] == before[request]
    [details] = _events(owner_session, "demo-a", maintenance.ACTION)
    assert details["assist_requests"] == 3
    assert _purge(app_db, dry_run=False)["demo-a"].assist_requests == 0  # nothing left


def test_delete_removes_old_requests_whose_run_is_gone(
    seeded: Seeded, owner_session: Session, app_db: Database
) -> None:
    _hold(owner_session, "demo-b")
    before = _texts(owner_session)
    dry = _delete(app_db, dry_run=True)["demo-a"]
    assert (dry.runs, dry.assist_requests, dry.assist_proposals) == (1, 2, 1)
    assert _texts(owner_session) == before
    done = _delete(app_db, dry_run=False)
    a = done["demo-a"]
    assert (a.runs, a.assist_requests, a.assist_proposals) == (1, 2, 1)
    assert done["demo-b"].legal_hold and done["demo-b"].assist_requests == 0
    left = _texts(owner_session)
    assert seeded.old_ended not in left and seeded.run_deleted not in left
    assert {seeded.recent, seeded.running, seeded.run_ended, seeded.b_old_ended} <= set(left)
    assert rows(owner_session, "SELECT count(*) FROM shifde.assist_proposals") == [(0,)]
    [details] = _events(owner_session, "demo-a", maintenance.DELETE_ACTION)
    assert (details["assist_requests"], details["assist_proposals"]) == (2, 1)


def test_each_clients_pass_touches_only_its_own_rows(
    seeded: Seeded, owner_session: Session, app_db: Database
) -> None:
    purged = _purge(app_db, dry_run=False)
    assert (purged["demo-a"].assist_requests, purged["demo-b"].assist_requests) == (3, 1)
    deleted = _delete(app_db, dry_run=False)
    assert (deleted["demo-a"].assist_requests, deleted["demo-b"].assist_requests) == (2, 1)
    [b_details] = _events(owner_session, "demo-b", maintenance.DELETE_ACTION)
    assert (b_details["assist_requests"], b_details["assist_proposals"]) == (1, 0)
    assert seeded.b_old_ended not in _texts(owner_session)


def test_a_newer_proposal_keeps_its_request(
    seeded: Seeded, owner_session: Session, app_db: Database
) -> None:
    owner_session.rollback()
    owner_session.execute(
        text("UPDATE shifde.assist_proposals SET created_at = now() - interval '1 day' "
             "WHERE id = :p"),
        {"p": seeded.proposal},
    )  # fmt: skip
    owner_session.commit()
    a = _delete(app_db, dry_run=False)["demo-a"]
    assert (a.assist_requests, a.assist_proposals) == (1, 0)
    assert seeded.run_deleted in _texts(owner_session)


# ------------------------------------------------------------------ the guard (#351, B-4010)
SIDES = pytest.mark.parametrize(("slug", "other"), [("demo-a", "demo-b"), ("demo-b", "demo-a")])
TABLES = ("assist_proposals", "assist_requests")
VALID = "now() - interval '100 days'"


def _pair(owner: Session, slug: str, age: str = OLD) -> dict[str, str]:
    """An ended (refused) request of `slug` and a proposal of it, both created `age` ago: id by
    table."""
    request = _request(owner, slug, "refused", age)
    proposal = str(uuid7())
    owner.execute(
        text("INSERT INTO shifde.assist_proposals (tenant_id, id, request_id, run_id, step_key, "
             "kind, payload, created_at) VALUES (:t, :id, :r, :run, 'k', 'evaluation_run', "
             "'{}'::jsonb, now() - CAST(:age AS interval))"),
        {"t": tenant_id(owner, slug), "id": proposal, "r": request, "run": uuid7(), "age": age},
    )  # fmt: skip
    owner.commit()
    return {"assist_proposals": proposal, "assist_requests": request}


def _policy(owner: Session, slug: str, data_class: str, days: int, hold: bool = False) -> None:
    owner.rollback()
    owner.execute(
        text("INSERT INTO shifde.retention_policies (tenant_id, data_class, retain_days, "
             "legal_hold) VALUES (:t, :c, :d, :h) ON CONFLICT (tenant_id, data_class) DO UPDATE "
             "SET retain_days = excluded.retain_days, legal_hold = excluded.legal_hold"),
        {"t": tenant_id(owner, slug), "c": data_class, "d": days, "h": hold},
    )  # fmt: skip
    owner.commit()


def _deleted(app_db: Database, tenant: UUID, table: str, row: str, cutoff: str) -> int:
    """Rows a DELETE of `row` removes as `shifde_app` in `tenant`'s context with the cutoff
    `cutoff` (a SQL expression on the database clock), rolled back."""
    with app_session(app_db, tenant) as s:
        s.execute(text("SELECT set_config('shifde.retention_delete_cutoff', "
                       f"CAST({cutoff} AS text), true)"))  # fmt: skip
        gone = s.execute(
            text(f"DELETE FROM shifde.{table} WHERE id = :id RETURNING 1"),  # nosec B608
            {"id": row},
        ).all()
        s.rollback()
    return len(gone)


@dataclass(frozen=True)
class Guarded:
    app_db: Database
    owner: Session
    slug: str
    tenant: UUID
    ids: dict[str, str]

    def refused(self, cutoff: str, message: str) -> None:
        """Both deletes fail with 42501 and `message`, and both rows stay."""
        for table in TABLES:
            with pytest.raises(DBAPIError) as caught:
                _deleted(self.app_db, self.tenant, table, self.ids[table], cutoff)
            assert getattr(caught.value.orig, "sqlstate", None) == "42501", caught.value
            assert message in str(caught.value.orig), table
            kept = rows(
                self.owner, f"SELECT 1 FROM shifde.{table} WHERE id = :i",  # nosec B608
                i=self.ids[table],
            )  # fmt: skip
            assert kept == [(1,)], table

    def admitted(self, cutoff: str) -> None:
        for table in TABLES:
            assert _deleted(self.app_db, self.tenant, table, self.ids[table], cutoff) == 1, table


def _guarded(app_db: Database, owner: Session, slug: str, age: str = OLD) -> Guarded:
    ids = _pair(owner, slug, age)
    return Guarded(app_db, owner, slug, UUID(str(tenant_id(owner, slug))), ids)


@SIDES
@pytest.mark.parametrize("data_class", ["run_artifacts", "usage_events"])
def test_the_guard_refuses_a_client_on_legal_hold(
    world: World, owner_session: Session, app_db: Database, slug: str, other: str, data_class: str
) -> None:
    mine = _guarded(app_db, owner_session, slug)
    theirs = _guarded(app_db, owner_session, other)
    _policy(owner_session, slug, data_class, 90, hold=True)
    mine.refused(VALID, "legal hold")
    theirs.admitted(VALID)  # my hold is mine alone
    _policy(owner_session, slug, data_class, 90, hold=False)
    mine.admitted(VALID)
    _policy(owner_session, other, data_class, 400, hold=True)  # nor does theirs hold me
    mine.admitted(VALID)


@SIDES
def test_the_guard_refuses_a_cutoff_inside_the_floor(
    world: World, owner_session: Session, app_db: Database, slug: str, other: str
) -> None:
    """The client's stored retention is at the floor (1 day), so the floor is the bound."""
    mine = _guarded(app_db, owner_session, slug)
    for data_class in maintenance.RUN_CLASSES:
        _policy(owner_session, slug, data_class, 1)
        _policy(owner_session, other, data_class, 400, hold=True)
    for cutoff in ("now() - interval '1 day' + interval '1 minute'", "now() + interval '1 year'"):
        mine.refused(cutoff, "retention floor")
    mine.admitted("now() - interval '1 day' - interval '1 minute'")


@SIDES
@pytest.mark.parametrize("data_class", maintenance.RUN_CLASSES)
def test_the_guard_refuses_a_cutoff_inside_the_clients_stored_retention(
    world: World, owner_session: Session, app_db: Database, slug: str, other: str, data_class: str
) -> None:
    """`retain_days = 400` of either run class refuses a cutoff 200 days ago on rows 500 days
    old; with 90 stored the same delete succeeds, and so does a cutoff 401 days ago."""
    cutoff = "now() - interval '200 days'"
    mine = _guarded(app_db, owner_session, slug, "500 days")
    theirs = _guarded(app_db, owner_session, other, "500 days")
    _policy(owner_session, slug, data_class, 400)
    mine.refused(cutoff, "client's retention")
    mine.admitted("now() - interval '401 days'")
    theirs.admitted(cutoff)  # my retention is mine alone
    _policy(owner_session, slug, data_class, 90)
    _policy(owner_session, other, data_class, 400)  # nor does theirs keep my rows
    mine.admitted(cutoff)


@SIDES
def test_the_guard_applies_the_default_retention_without_a_stored_row(
    world: World, owner_session: Session, app_db: Database, slug: str, other: str
) -> None:
    """No stored row: 90 days. A cutoff 89 days ago is refused and 91 days ago is not; with both
    run classes stored at 30 days, 89 days ago is admitted. The other client's rows (30 days, then
    400) change nothing."""
    cutoff = "now() - interval '89 days'"
    mine = _guarded(app_db, owner_session, slug)
    for data_class in maintenance.RUN_CLASSES:
        _policy(owner_session, other, data_class, 30)
    mine.refused(cutoff, "client's retention")
    for data_class in maintenance.RUN_CLASSES:
        _policy(owner_session, other, data_class, 400)
    mine.admitted("now() - interval '91 days'")
    for data_class in maintenance.RUN_CLASSES:
        _policy(owner_session, slug, data_class, 30)
    mine.admitted(cutoff)


@SIDES
def test_the_other_clients_context_deletes_none_of_a_clients_rows(
    world: World, owner_session: Session, app_db: Database, slug: str, other: str
) -> None:
    mine = _guarded(app_db, owner_session, slug)
    tenant_other = UUID(str(tenant_id(owner_session, other)))

    def state() -> list[tuple[Any, ...]]:
        return [
            row
            for table in TABLES
            for row in rows(
                owner_session,
                f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY id",  # nosec B608
                t=mine.tenant,
            )
        ]

    before = state()
    assert len(before) == 2
    with app_session(app_db, tenant_other) as s:
        s.execute(text("SELECT set_config('shifde.retention_delete_cutoff', "
                       f"CAST({VALID} AS text), true)"))  # fmt: skip
        for table in TABLES:
            for where in ("id = :id", "tenant_id = :t"):
                gone = s.execute(
                    text(f"DELETE FROM shifde.{table} WHERE {where} RETURNING 1"),  # nosec B608
                    {"id": mine.ids[table], "t": mine.tenant},
                ).all()
                assert gone == [], (table, where)
        s.commit()
    assert state() == before
    mine.admitted(VALID)  # in the owning client's context the same deletes succeed
