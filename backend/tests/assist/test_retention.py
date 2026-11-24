"""SP7-4 (Call S7-J, design section 8): `maintenance purge` and `delete` cover assist requests.

Purge blanks the text and context of a request older than the cutoff that is routed, refused or
failed and whose run is gone or has ended; a newer request and one whose run still runs keep
theirs. Delete, after the run families, removes such a request whose run is gone (the run the
same pass deleted included) and its proposals, and keeps one whose run still exists. `--dry-run`
counts and changes nothing; a client on legal hold is skipped; each client's pass counts and
touches only its own rows; the counts are in the audit details. Both run as `shifde_app` on a
database of the test's own (the full-app harness), rows seeded as the schema owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: row values
from uuid import UUID

import pytest
from sqlalchemy import text
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
