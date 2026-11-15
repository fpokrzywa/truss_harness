"""Runtime part 5 (RT-5): `runtime/schedules.py`, the pipeline schedule service (P6 section 4;
cron execution is not enabled, OD-8, so there is no due-schedule claim to test).

Behaviour: every call answers `scheduling_not_enabled` until the deployment turns the feature on;
a schedule is created for a pipeline the caller may update, as a service account of the client
whose permissions the caller holds, and listed under its pipeline.

Isolation: in A's context, B's pipeline, B's pinned version and B's service account answer
exactly as unknown ids, as `shifde_app` and as the schema owner (no row-level security); B's
schedules and audit rows are unchanged; A's own ids give another answer (not vacuous); A's list
holds only A's schedules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.build import repository as build_repo
from shifde.core.db import Database
from shifde.core.errors import AppError, SchedulingNotEnabled
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import schemas as rs
from shifde.runtime.schedules import ScheduleService
from tests._support.tenants import add_tenants
from tests.build.env_support import add_published
from tests.build.service_support import REGISTRY, add_person, service_for
from tests.runtime.conftest import app_session

MODES = ["app-role", "schema-owner"]
CRON = "0 * * * *"


@dataclass(frozen=True)
class Client:
    tenant: UUID
    user: UUID
    pipeline: UUID
    version: UUID
    account: UUID


def _client(o: Session, tenant: UUID) -> Client:
    user = add_person(o, tenant, "scheduler", "tenant_administrator")
    pipeline, version = add_published(o, tenant, "pipelines", f"pipe-{uuid7().hex}", {})
    build_repo.refresh_latest_published(o, tenant, pipeline)
    account = uuid7()
    o.execute(
        text("INSERT INTO shifde.service_accounts (id, tenant_id, name) VALUES (:i, :t, 'sa')"),
        {"i": account, "t": tenant},
    )
    return Client(tenant, user, pipeline, version, account)


@pytest.fixture
def pair(owner_db: Database) -> tuple[Client, Client]:
    a, b = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, a, b)
        out = _client(o, a), _client(o, b)
        o.commit()
    return out


@pytest.fixture(params=MODES)
def opened(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Callable[[UUID], Any]:
    @contextmanager
    def session(tenant: UUID) -> Iterator[Session]:
        cm = app_session(app_db, tenant) if request.param == "app-role" else owner_db.session()
        with cm as s:
            yield s

    return session


def _service(s: Session, c: Client, settings: Settings, *, enabled: bool = True) -> ScheduleService:
    arts = service_for(s, c.tenant, c.user, settings)
    on = settings.model_copy(update={"feature_pipeline_schedules": enabled})
    return ScheduleService(arts.sc, IntegrationRuntime(settings=on, registry=REGISTRY))


def _body(account: UUID, version: UUID | None = None) -> rs.ScheduleCreate:
    return rs.ScheduleCreate(
        cron=CRON,
        run_as_service_account_id=account,
        version_policy="pinned" if version else "latest_published",
        pipeline_version_id=version,
    )


def _state(o: Session, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    o.rollback()
    return (
        tuple(
            map(
                tuple,
                o.execute(
                    text(
                        "SELECT id, pipeline_id, status FROM shifde.pipeline_schedules "
                        "WHERE tenant_id = :t ORDER BY id"
                    ),
                    {"t": tenant},
                ).all(),
            )
        ),
        o.execute(
            text("SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one(),
    )


def test_schedules_answer_not_enabled_until_the_deployment_turns_them_on(
    pair: tuple[Client, Client], opened: Any, settings: Settings
) -> None:
    a, _ = pair
    with opened(a.tenant) as s:
        svc = _service(s, a, settings, enabled=False)
        with pytest.raises(SchedulingNotEnabled):
            svc.list(a.pipeline)
        with pytest.raises(SchedulingNotEnabled):
            svc.create(a.pipeline, _body(a.account))


def test_a_schedule_is_created_and_listed_under_its_pipeline_only_in_its_client(
    pair: tuple[Client, Client], opened: Any, settings: Settings, owner_db: Database
) -> None:
    a, b = pair
    with opened(b.tenant) as s:
        theirs = _service(s, b, settings).create(b.pipeline, _body(b.account))
    with opened(a.tenant) as s:
        svc = _service(s, a, settings)
        mine = svc.create(a.pipeline, _body(a.account, a.version))
        assert (mine.pipeline_id, mine.pipeline_version_id) == (a.pipeline, a.version)
        assert mine.run_as_service_account_id == a.account and mine.status == "paused"
        assert [x.id for x in svc.list(a.pipeline)] == [mine.id]
    with owner_db.session() as o:
        assert _state(o, b.tenant)[0] == ((theirs.id, b.pipeline, "paused"),)
        assert _state(o, a.tenant)[0] == ((mine.id, a.pipeline, "paused"),)


Case = Callable[[ScheduleService, Client, Client], object]
CASES: dict[str, Case] = {
    "list (pipeline)": lambda svc, a, t: svc.list(t.pipeline),
    "create (pipeline)": lambda svc, a, t: svc.create(t.pipeline, _body(a.account)),
    "create (pinned version)": lambda svc, a, t: svc.create(
        a.pipeline, _body(a.account, t.version)
    ),
    "create (service account)": lambda svc, a, t: svc.create(a.pipeline, _body(t.account)),
}


def _answer(s: Session, svc: ScheduleService, case: Case, a: Client, t: Client) -> str:
    try:
        result = case(svc, a, t)
    except AppError as exc:
        s.rollback()
        return f"{type(exc).__name__}: {exc.detail}"
    return type(result).__name__


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_naming_bs_ids_gets_the_unknown_id_answer(
    name: str,
    pair: tuple[Client, Client],
    opened: Any,
    settings: Settings,
    owner_db: Database,
) -> None:
    a, b = pair
    unknown = Client(uuid7(), uuid7(), uuid7(), uuid7(), uuid7())
    with owner_db.session() as o:
        b_before, a_before = _state(o, b.tenant), _state(o, a.tenant)
    with opened(a.tenant) as s:
        svc = _service(s, a, settings)
        for_b = _answer(s, svc, CASES[name], a, b)
        for_unknown = _answer(s, svc, CASES[name], a, unknown)
    assert for_b == for_unknown, (for_b, for_unknown)
    with owner_db.session() as o:
        assert _state(o, b.tenant) == b_before, "B's rows changed"
        assert _state(o, a.tenant) == a_before, "B's ids changed A's rows"
    with opened(a.tenant) as s:
        for_own = _answer(s, _service(s, a, settings), CASES[name], a, a)
    assert for_own != for_unknown, for_own


def test_every_public_method_is_a_case() -> None:
    public = {n for n in vars(ScheduleService) if not n.startswith("_") and n != "session"}
    assert public == {n.split(" ")[0] for n in CASES}


_SA_KEY = (
    "fk_pipeline_schedules__sa FOREIGN KEY (tenant_id, run_as_service_account_id) "
    "REFERENCES shifde.service_accounts (tenant_id, id)"
)


@contextmanager
def _without_the_service_account_key(owner_db: Database) -> Iterator[None]:
    """Drop `fk_pipeline_schedules__sa` (committed: the service commits on its own session) in
    this worker's test database, and restore it after (rows it would refuse are removed first)."""
    with owner_db.session() as o:
        o.execute(
            text("ALTER TABLE shifde.pipeline_schedules DROP CONSTRAINT fk_pipeline_schedules__sa")
        )
        o.commit()
    try:
        yield
    finally:
        with owner_db.session() as o:
            o.execute(
                text(
                    "DELETE FROM shifde.pipeline_schedules p WHERE NOT EXISTS (SELECT 1 FROM "
                    "shifde.service_accounts a WHERE a.tenant_id = p.tenant_id "
                    "AND a.id = p.run_as_service_account_id)"
                )
            )
            o.execute(text(f"ALTER TABLE shifde.pipeline_schedules ADD CONSTRAINT {_SA_KEY}"))
            o.commit()


def test_bs_service_account_is_refused_by_the_service_not_only_by_the_key(
    pair: tuple[Client, Client], opened: Any, settings: Settings, owner_db: Database
) -> None:
    """With the composite key gone, the service's own client-scoped lookup still answers B's
    service account like an unknown one and writes no schedule naming it (defence in depth;
    the key alone would otherwise be the only guard)."""
    a, b = pair
    with _without_the_service_account_key(owner_db):
        with opened(a.tenant) as s:
            svc = _service(s, a, settings)
            for_b = _answer(s, svc, CASES["create (service account)"], a, b)
            for_unknown = _answer(s, svc, CASES["create (service account)"], a, Client(
                uuid7(), uuid7(), uuid7(), uuid7(), uuid7()))  # fmt: skip
        with owner_db.session() as o:
            named = o.execute(
                text(
                    "SELECT count(*) FROM shifde.pipeline_schedules "
                    "WHERE run_as_service_account_id = :sa"
                ),
                {"sa": b.account},
            ).scalar_one()
    assert for_b == for_unknown == "NotFound: service account not found"
    assert named == 0
