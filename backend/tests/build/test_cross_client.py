"""Cross-client isolation of the build repository (plan recipe step 5, B-1901/B-1902): with client
A's context set as `shifde_app`, client B's artifacts, versions, pins, publish requests, prompt
test runs and pipeline references cannot be read, changed or referenced, through the repository
functions and through raw SQL, and every repository function also filters on the tenant id
(checked with the context of the wrong client and with no row-level security reach: the owner
session).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.build import repository as repo
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests.build.conftest import World
from tests.ledger.util import add_model
from tests.tenancy.conftest import app_session


@dataclass(frozen=True)
class Side:
    tenant: UUID
    artifact: UUID
    published: UUID  # version 1, published
    draft: UUID  # version 2, draft
    request: UUID  # pending publish request on the draft
    run: UUID
    pipeline: UUID
    pipeline_version: UUID
    model: UUID
    service_account: UUID
    schedule: UUID


@dataclass(frozen=True)
class Seed:
    a: Side
    b: Side


def _artifact(s: Session, tenant: UUID, kind: str, key: str) -> UUID:
    artifact = uuid7()
    repo.insert_artifact(
        s,
        {
            "id": artifact,
            "tenant_id": tenant,
            "kind": kind,
            "workspace_id": None,
            "project_id": None,
            "key": key,
            "name": key,
            "description": None,
            "owner_principal_id": uuid7(),
            "visibility": "scope",
            "created_by": None,
        },
    )
    return artifact


def _version(s: Session, tenant: UUID, artifact: UUID, kind: str, no: int) -> UUID:
    version = uuid7()
    repo.insert_version(
        s,
        {
            "id": version,
            "tenant_id": tenant,
            "artifact_id": artifact,
            "kind": kind,
            "version_no": no,
            "spec": {"n": no},
            "created_by": None,
            "based_on_version_id": None,
        },
    )
    return version


def _run_values(
    tenant: UUID, artifact: UUID, version: UUID, model: UUID
) -> dict[str, object]:  # object: column values
    return {
        "id": uuid7(),
        "tenant_id": tenant,
        "artifact_id": artifact,
        "version_id": version,
        "model_id": model,
        "inputs": {},
        "output": "o",
        "usage": {},
        "latency_ms": 1,
        "evaluation": [],
        "verdict": "pass",
        "created_by": None,
    }


def _request_values(tenant: UUID, artifact: UUID, version: UUID) -> dict[str, object]:
    return {
        "id": uuid7(),
        "tenant_id": tenant,
        "artifact_id": artifact,
        "kind": "prompts",
        "version_id": version,
        "version_no": 2,
        "change_note": None,
        "spec_hash": "h",
        "requested_by": uuid7(),
    }


def _side(s: Session, tenant: UUID, label: str, service_account: UUID) -> Side:
    artifact = _artifact(s, tenant, "prompts", f"prompt-{label}")
    published = _version(s, tenant, artifact, "prompts", 1)
    s.execute(
        text(
            "UPDATE shifde.artifact_versions SET state = 'published', spec_hash = 'h', "
            "published_at = now() WHERE id = :i"
        ),
        {"i": published},
    )
    repo.refresh_latest_published(s, tenant, artifact)
    draft = _version(s, tenant, artifact, "prompts", 2)
    repo.insert_pins(s, tenant, draft, [(published, "prompt")])
    request_values = _request_values(tenant, artifact, draft)
    repo.insert_request(s, request_values)
    model = add_model(s, tenant, model_key=f"m-{label}")
    run_values = _run_values(tenant, artifact, published, model)
    repo.insert_run(s, run_values)
    pipeline = _artifact(s, tenant, "pipelines", f"pipeline-{label}")
    pipeline_version = _version(s, tenant, pipeline, "pipelines", 1)
    repo.insert_pipeline_refs(s, tenant, pipeline_version, [("n1", "model", model)])
    repo.insert_validation_report(s, tenant, pipeline_version, {"ok": True}, ["n1"])
    schedule = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.pipeline_schedules (id, tenant_id, pipeline_id, version_policy, "
            "cron, run_as_service_account_id, created_by) VALUES (:i, :t, :p, "
            "'latest_published', '0 * * * *', :sa, :sa)"
        ),
        {"i": schedule, "t": tenant, "p": pipeline, "sa": service_account},
    )
    return Side(
        tenant,
        artifact,
        published,
        draft,
        UUID(str(request_values["id"])),
        UUID(str(run_values["id"])),
        pipeline,
        pipeline_version,
        model,
        service_account,
        schedule,
    )


@pytest.fixture
def seed(owner_db: Database, world: World) -> Seed:
    with owner_db.session() as s:
        out = Seed(
            _side(s, world.a.tenant, "a", world.a.service_account),
            _side(s, world.b.tenant, "b", world.b.service_account),
        )
        s.commit()
    return out


# ------------------------------------------------------------------ reads
def _assert_sees_only_a(s: Session, seed: Seed) -> None:
    a, b = seed.a, seed.b
    t = a.tenant
    assert repo.get_artifact(s, t, a.artifact) is not None
    assert repo.get_artifact(s, t, b.artifact) is None
    for kind, mine in (("prompts", a.artifact), ("pipelines", a.pipeline)):
        listed = repo.list_artifacts(
            s,
            t,
            kind=kind,
            after=None,
            limit=50,
            scope_type=None,
            scope_id=None,
            status=None,
            q=None,
        )
        assert [r["id"] for r in listed] == [mine]
    assert repo.artifact_extras(s, t, [a.artifact, b.artifact]) == {a.artifact: (1, True)}
    assert repo.lock_artifact(s, t, b.artifact) is None
    assert repo.get_version(s, t, b.published) is None
    assert repo.get_version(s, t, b.draft, lock=True) is None
    assert repo.get_draft(s, t, b.artifact) is None
    assert repo.list_versions(s, t, b.artifact) == []
    assert repo.next_version_no(s, t, b.artifact) == 1
    assert repo.list_pins(s, t, b.draft) == []
    assert [r["version_id"] for r in repo.list_pins(s, t, a.draft)] == [a.published]
    assert repo.pinned_by_count(s, t, b.published) == 0
    assert repo.get_request(s, t, b.request) is None
    assert repo.pending_request_for(s, t, b.draft) is None
    assert [r["id"] for r in repo.list_requests(s, t, None, None, 50)] == [a.request]
    assert repo.list_runs(s, t, b.artifact, None, 50) == []
    assert [r["id"] for r in repo.list_runs(s, t, a.artifact, None, 50)] == [a.run]
    assert repo.list_pipeline_refs(s, t, b.pipeline_version) == []
    assert len(repo.list_pipeline_refs(s, t, a.pipeline_version)) == 1
    assert repo.get_validation_report(s, t, b.pipeline_version) is None
    assert repo.get_validation_report(s, t, a.pipeline_version) is not None


def test_a_reads_only_its_own_rows(app_db: Database, seed: Seed) -> None:
    with app_session(app_db, seed.a.tenant) as s:
        _assert_sees_only_a(s, seed)
        s.rollback()


def test_the_tenant_filter_holds_without_row_level_security(owner_db: Database, seed: Seed) -> None:
    """The schema owner is not subject to row-level security: only the explicit tenant id keeps
    A's calls away from B's rows."""
    with owner_db.session() as s:
        _assert_sees_only_a(s, seed)
        s.rollback()


def test_the_wrong_clients_context_with_a_ids_finds_nothing(app_db: Database, seed: Seed) -> None:
    """B's context and A's tenant id: row-level security hides A's rows, the filter hides B's."""
    with app_session(app_db, seed.b.tenant) as s:
        assert repo.get_artifact(s, seed.a.tenant, seed.a.artifact) is None
        assert repo.get_artifact(s, seed.a.tenant, seed.b.artifact) is None
        assert repo.list_requests(s, seed.a.tenant, None, None, 50) == []
        s.rollback()


# ------------------------------------------------------------------ writes
def _versions_of_b(owner_db: Database, seed: Seed) -> list[tuple[str, int]]:
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT state, row_version FROM shifde.artifact_versions "
                "WHERE tenant_id = :t ORDER BY version_no, artifact_id"
            ),
            {"t": seed.b.tenant},
        ).all()
        return [(r[0], r[1]) for r in rows]


@pytest.mark.parametrize("context", ["a", "b", "owner"])
def test_a_cannot_change_bs_rows_through_the_repository(
    app_db: Database, owner_db: Database, seed: Seed, context: str
) -> None:
    """A's tenant id with A's context, with B's context and with no row-level security at all."""
    before = _versions_of_b(owner_db, seed)
    a, b = seed.a.tenant, seed.b
    session_cm: AbstractContextManager[Session]
    if context == "owner":
        session_cm = owner_db.session()
    else:
        session_cm = app_session(app_db, a if context == "a" else b.tenant)
    with session_cm as s:
        assert repo.update_artifact(s, a, b.artifact, None, {"name": "taken"}) is None
        assert repo.update_version(s, a, b.draft, None, {"change_note": "taken"}) is None
        assert repo.set_state(s, a, b.published, "deprecated") is None
        assert repo.decide_request(s, a, b.request, "approved", uuid7(), utcnow(), "ok") is None
        repo.refresh_latest_published(s, a, b.artifact)
        s.commit()
    with owner_db.session() as o:
        assert (
            o.execute(
                text("SELECT name FROM shifde.artifacts WHERE id = :i"), {"i": b.artifact}
            ).scalar_one()
            == "prompt-b"
        )
        assert (
            o.execute(
                text("SELECT latest_published_version_id FROM shifde.artifacts WHERE id = :i"),
                {"i": b.artifact},
            ).scalar_one()
            == b.published
        )
        assert (
            o.execute(
                text("SELECT status FROM shifde.publish_requests WHERE id = :i"),
                {"i": b.request},
            ).scalar_one()
            == "pending"
        )
    assert _versions_of_b(owner_db, seed) == before


def _refused(s: Session, writes: list[Callable[[], object]], states: tuple[str, ...]) -> None:
    for write in writes:
        with pytest.raises(DBAPIError) as err:
            write()
            s.flush()
        assert getattr(err.value.orig, "sqlstate", None) in states
        s.rollback()


def test_a_cannot_insert_rows_for_b(app_db: Database, seed: Seed) -> None:
    """Writing B's tenant id is refused by row-level security (42501)."""
    a, b = seed.a, seed.b
    with app_session(app_db, a.tenant) as s:
        _refused(
            s,
            [
                lambda: _artifact(s, b.tenant, "prompts", "stolen"),
                lambda: repo.insert_request(s, _request_values(b.tenant, b.artifact, b.draft)),
                lambda: repo.insert_pins(s, b.tenant, b.draft, [(b.published, "tool")]),
                lambda: repo.insert_run(s, _run_values(b.tenant, b.artifact, b.published, b.model)),
                lambda: _version(s, b.tenant, b.artifact, "prompts", 9),
                lambda: repo.insert_pipeline_refs(
                    s, b.tenant, b.pipeline_version, [("n9", "model", b.model)]
                ),
                lambda: repo.insert_validation_report(s, b.tenant, b.draft, {}, []),
            ],
            ("42501",),
        )


def test_a_cannot_reference_bs_rows(app_db: Database, seed: Seed) -> None:
    """A row of A naming B's artifact, version or model is refused by the composite keys: the key
    includes the tenant, so B's ids do not exist for A."""
    a, b = seed.a, seed.b
    with app_session(app_db, a.tenant) as s:
        _refused(
            s,
            [
                lambda: _version(s, a.tenant, b.artifact, "prompts", 9),
                lambda: repo.insert_pins(s, a.tenant, a.draft, [(b.published, "tool")]),
                lambda: repo.insert_pipeline_refs(
                    s, a.tenant, a.pipeline_version, [("n2", "model", b.model)]
                ),
                lambda: repo.insert_pipeline_refs(
                    s, a.tenant, a.pipeline_version, [("n3", "prompt_version", b.published)]
                ),
                lambda: repo.insert_run(s, _run_values(a.tenant, b.artifact, b.published, a.model)),
                lambda: repo.insert_run(s, _run_values(a.tenant, a.artifact, a.published, b.model)),
                lambda: repo.insert_request(s, _request_values(a.tenant, b.artifact, b.draft)),
            ],
            ("23503",),
        )


_KINDS = ("pin", "ref", "report")


def _oracle_write(kind: str, b: Side, role: str) -> Callable[[Session, UUID, UUID], None]:
    """A's write of exactly B's key (B's seeded or uncommitted row) for the version `v` tried."""
    if kind == "pin":
        return lambda s, t, v: repo.insert_pins(s, t, v, [(b.published, role)])
    if kind == "ref":
        return lambda s, t, v: repo.insert_pipeline_refs(s, t, v, [("n1", "model", b.model)])
    return lambda s, t, v: repo.insert_validation_report(s, t, v, {"x": 1}, [])


def _state_of(
    app_db: Database, tenant: UUID, write: Callable[[Session, UUID, UUID], None], v: UUID
) -> str:
    with app_session(app_db, tenant) as s:
        return _state_of_in(s, tenant, write, v)


@pytest.mark.parametrize("kind", _KINDS)
def test_bs_version_and_an_unknown_id_give_a_the_same_outcome(
    app_db: Database, seed: Seed, kind: str
) -> None:
    """Primary keys are client-keyed (B-1905): A naming B's version that has a row, B's version
    without one, and an id that exists nowhere all fail the same way (23503), whatever the
    `ON CONFLICT DO NOTHING` could have told."""
    a, b = seed.a, seed.b
    write = _oracle_write(kind, b, "prompt")  # B seeded the pin (b.draft, b.published, prompt)
    outcomes = {
        _state_of(app_db, a.tenant, write, v) for v in (b.pipeline_version, b.draft, uuid7())
    }
    assert outcomes == {"23503"}


@pytest.mark.parametrize("kind", _KINDS)
def test_a_does_not_wait_on_bs_uncommitted_insert(
    app_db: Database, owner_db: Database, seed: Seed, kind: str
) -> None:
    """B's uncommitted row for a version id never blocks A's insert naming that id (no 55P03)."""
    a, b = seed.a, seed.b
    version = uuid7()
    other = {
        "report": "INSERT INTO shifde.pipeline_validation_reports (tenant_id, version_id, report) "
        "VALUES (:t, :v, '{}')",
        "pin": "INSERT INTO shifde.artifact_version_pins (tenant_id, version_id, "
        "pinned_version_id, role) VALUES (:t, :v, :p, 'tool')",
        "ref": "INSERT INTO shifde.pipeline_version_refs (tenant_id, pipeline_version_id, "
        "node_id, ref_type, ref_id, model_id) VALUES (:t, :v, 'n1', 'model', :m, :m)",
    }[kind]
    with owner_db.session() as o:
        spare = _artifact(o, b.tenant, "pipelines", "pipeline-spare")
        o.execute(
            text(
                "INSERT INTO shifde.artifact_versions (id, tenant_id, artifact_id, kind, "
                "version_no, state) VALUES (:v, :t, :a, 'pipelines', 1, 'draft')"
            ),
            {"v": version, "t": b.tenant, "a": spare},
        )
        o.commit()
        o.execute(
            text(other), {"t": b.tenant, "v": version, "p": b.published, "m": b.model}
        )  # not committed: holds the key
        with app_session(app_db, a.tenant) as s:
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            assert _state_of_in(s, a.tenant, _oracle_write(kind, b, "tool"), version) == "23503"
        o.rollback()


def _state_of_in(
    s: Session, tenant: UUID, write: Callable[[Session, UUID, UUID], None], v: UUID
) -> str:
    try:
        write(s, tenant, v)
        s.flush()
        return "ok"
    except DBAPIError as err:
        return str(getattr(err.orig, "sqlstate", None))
    finally:
        s.rollback()


def test_a_schedule_naming_bs_rows_is_refused(app_db: Database, seed: Seed) -> None:
    a, b = seed.a, seed.b
    insert = (
        "INSERT INTO shifde.pipeline_schedules (id, tenant_id, pipeline_id, version_policy, "
        "pipeline_version_id, cron, run_as_service_account_id, created_by) VALUES "
        "(gen_random_uuid(), :t, :p, :pol, :v, '0 * * * *', :sa, :sa)"
    )
    cases = (
        {"p": b.pipeline, "pol": "latest_published", "v": None, "sa": a.service_account},
        {"p": a.pipeline, "pol": "pinned", "v": b.pipeline_version, "sa": a.service_account},
        {"p": a.pipeline, "pol": "latest_published", "v": None, "sa": b.service_account},
    )
    with app_session(app_db, a.tenant) as s:
        for case in cases:
            with pytest.raises(DBAPIError) as err:
                s.execute(text(insert), {"t": a.tenant, **case})
            assert getattr(err.value.orig, "sqlstate", None) == "23503"
            s.rollback()


def test_without_a_client_context_an_insert_for_a_is_refused(app_db: Database, seed: Seed) -> None:
    with app_session(app_db, None) as s:
        with pytest.raises(DBAPIError) as err:
            _artifact(s, seed.a.tenant, "prompts", "no-context")
            s.flush()
        assert getattr(err.value.orig, "sqlstate", None) == "42501"
        s.rollback()


# ------------------------------------------------------------------ raw SQL as shifde_app
_TABLES = (
    "artifacts",
    "artifact_versions",
    "artifact_version_pins",
    "publish_requests",
    "prompt_test_runs",
    "pipeline_version_refs",
    "pipeline_validation_reports",
    "pipeline_schedules",
)


@pytest.mark.parametrize("table", _TABLES)
def test_raw_sql_as_the_app_role_sees_only_its_own_rows(
    app_db: Database, seed: Seed, table: str
) -> None:
    with app_session(app_db, seed.a.tenant) as s:
        mine = s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one()
        assert mine >= 1
        b_rows = s.execute(
            text(f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :b"),
            {"b": seed.b.tenant},
        ).scalar_one()
        assert b_rows == 0
        s.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE shifde.artifacts SET name = 'x' WHERE tenant_id = :b",
        "UPDATE shifde.artifacts SET name = 'x' WHERE id = :b_artifact",
        "UPDATE shifde.artifact_versions SET change_note = 'x' WHERE tenant_id = :b",
        "UPDATE shifde.artifact_versions SET state = 'deprecated' WHERE id = :b_published",
        "UPDATE shifde.publish_requests SET comment = 'x' WHERE tenant_id = :b",
        "DELETE FROM shifde.artifact_versions WHERE id = :b_draft",
    ],
)
def test_raw_sql_as_the_app_role_changes_nothing_of_b(
    app_db: Database, owner_db: Database, seed: Seed, sql: str
) -> None:
    params = {
        "b": seed.b.tenant,
        "b_artifact": seed.b.artifact,
        "b_published": seed.b.published,
        "b_draft": seed.b.draft,
    }
    with app_session(app_db, seed.a.tenant) as s:
        try:
            r = s.execute(text(sql), params)
            assert r.rowcount == 0  # type: ignore[attr-defined]  # mypy: CursorResult has it
        except DBAPIError as err:  # DELETE is not granted to the app role at all
            assert getattr(err.orig, "sqlstate", None) == "42501"
        s.rollback()
    with owner_db.session() as o:
        count = o.execute(
            text("SELECT count(*) FROM shifde.artifact_versions WHERE tenant_id = :t"),
            {"t": seed.b.tenant},
        ).scalar_one()
        assert count == 3  # published, draft, pipeline draft: untouched


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO shifde.artifacts (id, tenant_id, kind, key, name, owner_principal_id) "
        "VALUES (gen_random_uuid(), :b, 'prompts', 'raw-b', 'n', gen_random_uuid())",
        "INSERT INTO shifde.artifacts (id, tenant_id, kind, key, name, owner_principal_id) "
        "VALUES (gen_random_uuid(), NULL, 'prompts', 'raw-none', 'n', gen_random_uuid())",
        "INSERT INTO shifde.artifact_versions (id, tenant_id, artifact_id, kind, version_no, "
        "state) VALUES (gen_random_uuid(), :b, :b_artifact, 'prompts', 7, 'draft')",
    ],
)
def test_raw_sql_as_the_app_role_cannot_write_for_b_or_for_nobody(
    app_db: Database, seed: Seed, sql: str
) -> None:
    with app_session(app_db, seed.a.tenant) as s:
        with pytest.raises(DBAPIError) as err:
            s.execute(text(sql), {"b": seed.b.tenant, "b_artifact": seed.b.artifact})
        assert getattr(err.value.orig, "sqlstate", None) in ("42501", "23502")
        s.rollback()


def test_without_a_client_context_no_build_row_is_visible(app_db: Database, seed: Seed) -> None:
    with app_session(app_db, None) as s:
        for table in _TABLES:
            assert s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one() == 0
        s.rollback()
