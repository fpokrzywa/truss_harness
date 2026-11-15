"""`OntologyService` (part 2, B-2007..B-2012): isolation follow-ups F1, F2, F3 and F5 of ontology
isolation review 1 on the service, as `shifde_app` and as the schema owner (no row-level
security, so only the client id the service passes confines A)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any  # Any: rows and JSON
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.build import repository as build_repo
from shifde.core.db import Database
from shifde.core.errors import NotFound, ValidationFailed
from shifde.core.settings import Settings
from shifde.ontology import engine
from shifde.ontology import repository as repo
from shifde.ontology import schemas as s
from shifde.ontology.service import OntologyService
from tests.build.conftest import opener
from tests.build.service_support import service_for
from tests.ontology.test_routes_cross_client import TYPE, Side, Two, two

__all__ = ["opener", "two"]

Opener = Callable[[UUID], AbstractContextManager[Session]]


def _service(session: Session, side: Side, settings: Settings) -> OntologyService:
    return OntologyService(service_for(session, side.tenant, side.user, settings))


def _policies(owner_db: Database, tenant: UUID) -> list[Any]:  # Any: rows
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT id, ontology_id, object_type_key, row_filter, row_version "
                    "FROM shifde.ontology_access_policies WHERE tenant_id = :t ORDER BY id"
                ),
                {"t": tenant},
            ).all()
        )


def _body(user: UUID, version: int | None = None) -> s.PolicyIn:
    return s.PolicyIn(
        object_type_key=TYPE,
        subject=s.PolicySubject(type="user", id=user),
        row_filter="row.id > 0",
        row_version=version,
    )


# ------------------------------------------------------------------ F1
def test_a_policy_of_another_ontology_cannot_be_rewritten_or_deleted(
    opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    """A is allowed on both ontologies; naming `other`'s policy under `ontology` is the unknown
    policy answer, in the service and in the repository; nothing changes."""
    a = two.a
    before = _policies(owner_db, a.tenant)
    with opener(a.tenant) as session:
        svc = _service(session, a, settings)
        body = _body(a.user, 1)
        with pytest.raises(NotFound):
            svc.update_policy(UUID(a.ontology), UUID(a.other_policy), body)
        with pytest.raises(NotFound):
            svc.delete_policy(UUID(a.ontology), UUID(a.other_policy))
        session.rollback()
        # the repository refuses on its own, whatever the service checked
        values = {
            "object_type_key": TYPE,
            "subject_type": "user",
            "subject_id": a.user,
            "row_filter": "x",
        }
        assert (
            repo.update_policy(
                session, a.tenant, UUID(a.ontology), UUID(a.other_policy), None, values, ["id"]
            )
            is None
        )
        session.rollback()
        assert _policies(owner_db, a.tenant) == before
        # control: its own policy under its own ontology
        row = svc.update_policy(UUID(a.ontology), UUID(a.policy), body)
        assert row.row_filter == "row.id > 0" and row.row_version == 2


# ------------------------------------------------------------------ F2
def test_usage_reads_a_run_with_the_client_id(
    opener: Opener, two: Two, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`runs` is the runtime's table (not yet there): with the table assumed present the
    statement `_usage` sends names `tenant_id`, and without it no run is read at all."""
    a = two.a
    seen: list[str] = []
    with opener(a.tenant) as session:
        svc = _service(session, a, settings)
        art = svc.arts.artifact_row("ontologies", UUID(a.ontology))
        bind = session.get_bind()

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:  # Any: DBAPI
            seen.append(statement)

        event.listen(bind, "before_cursor_execute", record)
        try:
            svc._usage(art, uuid4(), uuid4(), "success", 1)  # no `runs` table: skipped
            assert not any("SELECT root_run_id" in q for q in seen)
            monkeypatch.setattr(OntologyService, "_has_runs_table", lambda self: True)
            with pytest.raises(DBAPIError):  # the table does not exist yet
                svc._usage(art, uuid4(), uuid4(), "success", 1)
        finally:
            event.remove(bind, "before_cursor_execute", record)
            session.rollback()
    runs = [q for q in seen if "shifde.runs" in q and "SELECT root_run_id" in q]
    assert runs and all("tenant_id" in q for q in runs), seen


def test_a_run_id_must_be_a_run_of_the_client_and_the_unchecked_path_needs_one(
    opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    """F4 half and B-2008: a run id nobody owns, or no run id on the unchecked path (`api=False`),
    is refused before the ontology is read; no lineage row is written."""
    a = two.a
    body = s.QueryIn(object_type=TYPE, select=["id"])
    with owner_db.session() as o:
        count = text("SELECT count(*) FROM shifde.ontology_query_lineage WHERE tenant_id = :t")
        before = o.execute(count, {"t": a.tenant}).scalar_one()
    with opener(a.tenant) as session:
        svc = _service(session, a, settings)
        with pytest.raises(NotFound):
            svc.query(UUID(a.ontology), body, run_id=uuid4())
        with pytest.raises(NotFound):
            svc.query(UUID(a.ontology), body, api=False)
        with pytest.raises(NotFound):
            svc.get_object(UUID(a.ontology), TYPE, "1", api=False, run_id=uuid4())
        with pytest.raises(NotFound):
            svc.action_inputs(UUID(a.ontology), uuid4(), "act", "1", api=False)
        session.rollback()
    with owner_db.session() as o:
        assert o.execute(count, {"t": a.tenant}).scalar_one() == before


# ------------------------------------------------------------------ F3
def test_frozen_mappings_need_a_version_of_the_callers_client(
    opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    a, b = two.a, two.b
    with owner_db.session() as o:
        theirs = build_repo.get_artifact(o, b.tenant, UUID(b.ontology))
        assert theirs is not None
        version = (
            o.execute(
                text("SELECT * FROM shifde.artifact_versions WHERE artifact_id = :a"),
                {"a": theirs["id"]},
            )
            .mappings()
            .one()
        )
    with opener(a.tenant) as session:
        svc = _service(session, a, settings)
        with pytest.raises(NotFound):
            svc._frozen(version)
        # A's own version of the same shape is the control
        mine = svc._version(svc.arts.artifact("ontologies", UUID(a.ontology)), None)
        spec, mappings = svc._frozen(mine)
        assert TYPE in {k[1] for k in mappings} and spec["object_types"]


def test_a_mapping_of_another_client_gets_no_schema_discovery(
    opener: Opener, two: Two, settings: Settings, owner_db: Database
) -> None:
    a, b = two.a, two.b
    with owner_db.session() as o:
        b_mapping = dict(
            o.execute(
                text("SELECT * FROM shifde.ontology_source_mappings WHERE tenant_id = :t"),
                {"t": b.tenant},
            )
            .mappings()
            .first()
            or {}
        )
        a_mapping = dict(
            o.execute(
                text("SELECT * FROM shifde.ontology_source_mappings WHERE tenant_id = :t"),
                {"t": a.tenant},
            )
            .mappings()
            .first()
            or {}
        )
    with opener(a.tenant) as session:
        svc = _service(session, a, settings)
        assert svc._schema_discovery(b_mapping) is None  # B's row
        with pytest.raises(NotFound):  # A's row naming B's connection id
            svc._schema_discovery({**a_mapping, "connection_id": b_mapping["connection_id"]})
        found = svc._schema_discovery(a_mapping)  # control
        assert found is not None and found["tables"]
        with pytest.raises(NotFound):
            svc._conn_row(b_mapping["connection_id"])
        assert svc._conn_row(a_mapping["connection_id"])["tenant_id"] == a.tenant


def test_attributes_are_read_with_the_client_id(
    opener: Opener, two: Two, settings: Settings
) -> None:
    a, b = two.a, two.b
    with opener(a.tenant) as session:
        svc = _service(session, a, settings)
        assert [r.principal_id for r in svc.list_attributes()] == [a.user]
        with pytest.raises(NotFound):
            svc.get_attributes("user", b.user)
        with pytest.raises(NotFound):
            svc.put_attributes("user", b.user, s.AttributesIn(attributes={"x": 1}))
        assert svc.get_attributes("user", a.user).attributes == {"region": "a"}


# ------------------------------------------------------------------ F-04 and F5
OT = {
    "primary_key": ["id", "ref"],
    "properties": [
        {"key": "id", "type": "number"},
        {"key": "ref", "type": "integer"},
    ],
}


def _typed(raw: str) -> list[Any]:  # Any: typed values
    return OntologyService._typed_pk(object.__new__(OntologyService), OT, raw)


def test_typed_identifiers_keep_numbers_above_2_to_the_53_exact() -> None:
    first, second = _typed("9007199254740992,1"), _typed("9007199254740993,1")
    assert first[0] == 9007199254740992 and second[0] == 9007199254740993
    assert first[0] != second[0] and isinstance(second[0], int)
    assert _typed("1.5,2") == [1.5, 2]
    assert _typed("0.1,2") == [0.1, 2]
    exact = _typed("12345678901234567890.123456789012345,2")[0]
    assert isinstance(exact, Decimal) and str(exact) == "12345678901234567890.123456789012345"
    with pytest.raises(ValidationFailed):
        _typed("abc,2")
    with pytest.raises(ValidationFailed):
        _typed("1")  # wrong number of parts


def test_a_huge_integer_literal_is_exact_and_not_a_server_error() -> None:
    """F5: `int(text)` raises above 4300 digits; the engine falls back to `Decimal`."""
    digits = "9" * 5000
    value = engine.exact_number(digits)
    assert isinstance(value, Decimal) and value == Decimal(digits)
    assert engine.exact_number("-" + digits) == Decimal("-" + digits)  # (unary minus would round)
    assert engine.exact_number("9" * 4300) == int("9" * 4300)  # still an int at the limit
    assert _typed(f"{digits},1")[0] == Decimal(digits)
