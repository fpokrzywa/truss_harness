"""Issue #34 (REQ F-12 follow-up): the reaper tick sweeps capture-off clients for ended runs whose
scrub never ran (`Worker._sweep_capture_off`).

Behaviour: an ended run of a capture-off client that still holds content (its scrub never ran) is
scrubbed by the sweep, sizes and hashes kept; a capture-on client's ended run is untouched; a
terminal child under a running root keeps its content; a second sweep changes nothing and adds no
audit row.

Isolation: one sweep over capture-off clients A and B and capture-on client C scrubs A's and B's
runs, each in its own client's context (`app.tenant_id` equals the scrubbed client), and leaves C's
rows byte-for-byte unchanged; a failure while sweeping A rolls A back, is logged and does not stop
B's sweep or escape `_reap`.

The sweep is driven on a `Worker` (the app role); the database is seeded and read as the owner.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from tests.runtime.test_capture_off import (
    ARGUMENT,
    INPUT,
    SCRUBBED,
    _arguments,
    _artifacts,
    _client,
    _seed,
    _snapshot,
)
from tests.runtime.test_worker import _worker


def _ended(owner_db: Database, capture: str | None) -> tuple[UUID, UUID]:
    """A client with one failed run whose content was never scrubbed: (tenant, run)."""
    tenant, user = _client(owner_db, capture)
    with owner_db.session() as o:
        x = _seed(o, tenant, user, status="failed")
        o.commit()
    return tenant, x.run


def _sweep(settings: Settings) -> None:
    w = _worker(settings, "w-sweep")
    try:
        w._reap()
    finally:
        w.db.dispose()


def _audit_count(owner_db: Database, tenants: list[UUID]) -> int:
    with owner_db.session() as o:
        return int(
            o.execute(
                text("SELECT count(*) FROM shifde.audit_events WHERE tenant_id = ANY(:t)"),
                {"t": tenants},
            ).scalar_one()
        )


def _assert_swept(owner_db: Database, run: UUID, before: list[tuple[Any, ...]]) -> None:
    after = _artifacts(owner_db, run)
    assert after and all(a[3] == SCRUBBED for a in after)
    assert [(a[0], a[4], a[5]) for a in after] == [(b[0], b[4], b[5]) for b in before]
    args = _arguments(owner_db, run)
    assert args and all(a is None for a in args)


def test_a_straggler_is_scrubbed(owner_db: Database, settings: Settings) -> None:
    tenant, run = _ended(owner_db, "metadata")
    before = _artifacts(owner_db, run)
    assert [a[3] for a in before][0] == {"input": INPUT}
    assert _arguments(owner_db, run) == [{"item": ARGUMENT}]
    _sweep(settings)
    _assert_swept(owner_db, run, before)


def test_capture_on_is_untouched(owner_db: Database, settings: Settings) -> None:
    tenant, _ = _ended(owner_db, "full")
    before = _snapshot(owner_db, tenant)
    assert INPUT in repr(before) and ARGUMENT in repr(before)
    _sweep(settings)
    assert _snapshot(owner_db, tenant) == before


def test_a_run_under_a_running_root_is_left_alone(owner_db: Database, settings: Settings) -> None:
    tenant, user = _client(owner_db, "metadata")
    with owner_db.session() as o:
        root = _seed(o, tenant, user)
        child = _seed(o, tenant, user, status="failed", parent_run_id=root.run,
                      root_run_id=root.run, depth=1)  # fmt: skip
        o.commit()
    before = _artifacts(owner_db, child.run)
    _sweep(settings)
    assert _artifacts(owner_db, child.run) == before
    assert _arguments(owner_db, child.run) == [{"item": ARGUMENT}]


def test_clients_are_isolated(
    owner_db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_tenant, a_run = _ended(owner_db, "metadata")
    b_tenant, b_run = _ended(owner_db, None)  # unset: off outside development
    c_tenant, _ = _ended(owner_db, "full")
    assert not settings.is_development
    a_before, b_before = _artifacts(owner_db, a_run), _artifacts(owner_db, b_run)
    c_before = _snapshot(owner_db, c_tenant)
    calls: list[tuple[UUID, str | None]] = []
    scrub = repo.scrub_run_content

    def recording(session: Session, tenant_id: UUID, run_id: UUID) -> None:
        calls.append(
            (tenant_id, session.execute(text("SELECT current_setting('app.tenant_id', true)"))
             .scalar_one())
        )  # fmt: skip
        scrub(session, tenant_id, run_id)

    monkeypatch.setattr(repo, "scrub_run_content", recording)
    _sweep(settings)
    _assert_swept(owner_db, a_run, a_before)
    _assert_swept(owner_db, b_run, b_before)
    assert _snapshot(owner_db, c_tenant) == c_before
    assert {a_tenant, b_tenant} <= {t for t, _ in calls}
    assert c_tenant not in {t for t, _ in calls}
    assert all(setting == str(tenant) for tenant, setting in calls)


def test_one_clients_failure_does_not_stop_the_others(
    owner_db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_tenant, a_run = _ended(owner_db, "metadata")
    b_tenant, b_run = _ended(owner_db, "metadata")
    a_before, b_before = _snapshot(owner_db, a_tenant), _artifacts(owner_db, b_run)
    scrub = repo.scrub_run_content

    def failing(session: Session, tenant_id: UUID, run_id: UUID) -> None:
        scrub(session, tenant_id, run_id)
        if tenant_id == a_tenant:
            raise RuntimeError("sweep boom")

    monkeypatch.setattr(repo, "scrub_run_content", failing)
    _sweep(settings)  # raises nothing
    assert _snapshot(owner_db, a_tenant) == a_before  # rolled back
    _assert_swept(owner_db, b_run, b_before)
    assert _arguments(owner_db, a_run) == [{"item": ARGUMENT}]


def test_the_sweep_is_idempotent(owner_db: Database, settings: Settings) -> None:
    tenants = [_ended(owner_db, mode)[0] for mode in ("metadata", None, "full")]
    _sweep(settings)
    before = [_snapshot(owner_db, t) for t in tenants]
    audits = _audit_count(owner_db, tenants)
    _sweep(settings)
    assert [_snapshot(owner_db, t) for t in tenants] == before
    assert _audit_count(owner_db, tenants) == audits
