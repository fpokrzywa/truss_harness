"""Cross-client isolation of the ontology data layer (plan recipe step 5, B-2002): every function of
`ontology.repository` and the ontology functions of `build.repository` (mappings, version info),
with client A's tenant id, against client B's rows.

Each case runs as `shifde_app` in A's context, as `shifde_app` in B's context (A's tenant id, so
row-level security hides A's rows and the explicit filter hides B's) and as the schema owner (no
row-level security: only the explicit `tenant_id` keeps A away from B). B's ids get the same
outcome as ids that exist nowhere; A's own rows are the positive control.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, NoResultFound
from sqlalchemy.orm import Session

from shifde.build import repository as build_repo
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.ontology import repository as repo
from tests.connections.conftest import connection_values
from tests.ontology.conftest import World
from tests.tenancy.conftest import app_session

CONTEXTS = ("a", "b", "owner")  # whose session runs A's calls
POLICY = {"object_type_key": "x.customer", "subject_type": "role", "row_filter": None}


@dataclass(frozen=True)
class Side:
    tenant: UUID
    ontology: UUID  # artifact of kind ontologies
    published: UUID  # version 1, published (its mapping and info are frozen)
    draft: UUID  # version 2, draft
    connection: UUID
    mapping_published: UUID
    mapping_draft: UUID
    policy: UUID
    principal: UUID
    lineage: UUID
    spare_connection: UUID  # a second connection, for the update tests


@dataclass(frozen=True)
class Seed:
    a: Side
    b: Side


def _mapping_values(
    tenant: UUID, version: UUID, connection: UUID, key: str, **over: Any
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": uuid7(),
        "tenant_id": tenant,
        "ontology_version_id": version,
        "target_kind": "object_type",
        "target_key": key,
        "connection_id": connection,
        "relation_schema": "public",
        "relation_name": "customers",
        "column_map": {"id": "id"},
        "key_map": None,
        "filters": [],
        "status": "unchecked",
        "created_by": None,
    }
    values.update(over)
    return values


def _policy_values(tenant: UUID, ontology: UUID, subject: UUID) -> dict[str, Any]:
    return {
        "id": uuid7(),
        "tenant_id": tenant,
        "ontology_id": ontology,
        "property_keys": None,
        "subject_id": subject,
        "created_by": None,
        **POLICY,
    }


def _lineage_values(
    tenant: UUID, ontology: UUID, version: UUID, connection: UUID
) -> dict[str, Any]:
    return {
        "id": uuid7(),
        "tenant_id": tenant,
        "principal_id": uuid7(),
        "principal_type": "user",
        "ontology_id": ontology,
        "ontology_version_id": version,
        "run_id": None,
        "channel": "api",
        "object_types": ["x.customer"],
        "connection_ids": [connection],
        "compiled_query_hashes": ["h"],
        "row_counts": {"x.customer": 1},
        "duration_ms": 3,
        "outcome": "success",
        "error_code": None,
    }


def _side(s: Session, tenant: UUID, label: str) -> Side:
    ontology = uuid7()
    build_repo.insert_artifact(
        s,
        {
            "id": ontology,
            "tenant_id": tenant,
            "kind": "ontologies",
            "workspace_id": None,
            "project_id": None,
            "key": f"onto-{label}",
            "name": f"onto-{label}",
            "description": None,
            "owner_principal_id": uuid7(),
            "visibility": "scope",
            "created_by": None,
        },
    )

    def version(no: int) -> UUID:
        vid = uuid7()
        build_repo.insert_version(
            s,
            {
                "id": vid,
                "tenant_id": tenant,
                "artifact_id": ontology,
                "kind": "ontologies",
                "version_no": no,
                "spec": {"n": no},
                "created_by": None,
                "based_on_version_id": None,
            },
        )
        return vid

    connection_row = connection_values(tenant)
    conn_repo.insert_connection(s, connection_row)
    connection = UUID(str(connection_row["id"]))
    spare_row = connection_values(tenant)
    conn_repo.insert_connection(s, spare_row)
    published = version(1)
    mapping_published = build_repo.upsert_mapping(
        s, tenant, _mapping_values(tenant, published, connection, "x.customer")
    )["id"]
    build_repo.insert_ontology_info(s, tenant, published, f"hash-{label}", {"ok": True})
    s.execute(
        text(
            "UPDATE shifde.artifact_versions SET state = 'published', spec_hash = 'h', "
            "published_at = now() WHERE id = :i"
        ),
        {"i": published},
    )
    draft = version(2)
    mapping_draft = build_repo.upsert_mapping(
        s, tenant, _mapping_values(tenant, draft, connection, "x.order")
    )["id"]
    policy = repo.insert_policy(s, tenant, _policy_values(tenant, ontology, uuid7()))["id"]
    principal = uuid7()
    repo.put_attributes(s, tenant, "user", principal, {"region": label}, None, uuid7())
    lineage_values = _lineage_values(tenant, ontology, published, connection)
    repo.insert_lineage(s, tenant, lineage_values)
    s.execute(
        text(
            "INSERT INTO shifde.ontology_materializations (id, tenant_id, ontology_version_id, "
            "object_type_key) VALUES (:i, :t, :v, 'x.customer')"
        ),
        {"i": uuid7(), "t": tenant, "v": published},
    )
    return Side(
        tenant,
        ontology,
        published,
        draft,
        connection,
        mapping_published,
        mapping_draft,
        policy,
        principal,
        lineage_values["id"],
        UUID(str(spare_row["id"])),
    )


@pytest.fixture
def seed(owner_db: Database, world: World) -> Seed:
    with owner_db.session() as s:
        out = Seed(_side(s, world.a.tenant, "a"), _side(s, world.b.tenant, "b"))
        s.commit()
    return out


@pytest.fixture(params=CONTEXTS)
def session(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database, seed: Seed
) -> Iterator[Session]:
    """A session that runs A's calls (A's tenant id is the argument of every call)."""
    cm: AbstractContextManager[Session]
    if request.param == "owner":
        cm = owner_db.session()
    else:
        cm = app_session(app_db, seed.a.tenant if request.param == "a" else seed.b.tenant)
    with cm as s:
        yield s
        s.rollback()


@pytest.fixture(params=("a", "owner"))
def writer(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database, seed: Seed
) -> Iterator[Session]:
    """A session for A's inserts (B's context would refuse them all with 42501 first)."""
    cm: AbstractContextManager[Session]
    cm = owner_db.session() if request.param == "owner" else app_session(app_db, seed.a.tenant)
    with cm as s:
        yield s
        s.rollback()


@pytest.fixture
def context(session: Session, request: pytest.FixtureRequest) -> str:
    return str(request.node.callspec.params["session"])


# ------------------------------------------------------------------ reads
def test_reads_find_only_a_and_b_gets_the_unknown_id_answer(
    session: Session, context: str, seed: Seed
) -> None:
    a, b = seed.a, seed.b
    t, unknown = a.tenant, uuid7()
    mine = context != "b"  # B's context hides A's rows too
    # policies
    assert [p["id"] for p in repo.list_policies(session, t, a.ontology)] == (
        [a.policy] if mine else []
    )
    assert repo.list_policies(session, t, b.ontology) == repo.list_policies(session, t, unknown)
    assert repo.list_policies(session, t, b.ontology) == []
    assert (repo.get_policy(session, t, a.ontology, a.policy) is not None) is mine
    assert repo.get_policy(session, t, b.ontology, b.policy) is None
    assert repo.get_policy(session, t, a.ontology, b.policy) is None  # A's ontology, B's policy
    assert repo.get_policy(session, t, a.ontology, unknown) is None
    # principal attributes
    assert [r["principal_id"] for r in repo.list_attributes(session, t)] == (
        [a.principal] if mine else []
    )
    assert repo.get_attributes(session, t, "user", b.principal) is None
    assert repo.get_attributes(session, t, "user", unknown) is None
    assert (repo.get_attributes(session, t, "user", a.principal) is not None) is mine
    # lineage
    assert [r["id"] for r in repo.list_lineage(session, t, a.ontology, None, None, 50)] == (
        [a.lineage] if mine else []
    )
    assert repo.list_lineage(session, t, b.ontology, None, None, 50) == []
    assert repo.get_lineage(session, t, b.lineage) is None
    assert repo.get_lineage(session, t, unknown) is None
    assert (repo.get_lineage(session, t, a.lineage) is not None) is mine
    # mappings and version info (build.repository)
    assert [r["id"] for r in build_repo.list_mappings(session, t, a.published)] == (
        [a.mapping_published] if mine else []
    )
    assert build_repo.list_mappings(session, t, b.published) == []
    assert build_repo.list_mappings(session, t, unknown) == []
    assert build_repo.get_mapping(session, t, b.published, "object_type", "x.customer") is None
    assert (
        build_repo.get_mapping(session, t, a.published, "object_type", "x.customer") is not None
    ) is mine
    assert build_repo.get_ontology_info(session, t, b.published) is None
    assert build_repo.get_ontology_info(session, t, unknown) is None
    info = build_repo.get_ontology_info(session, t, a.published)
    assert (info is not None and info["mappings_hash"] == "hash-a") is mine


def test_lineage_pages_do_not_cross_clients(session: Session, context: str, seed: Seed) -> None:
    """The `after` cursor of a page cannot walk into B's rows: with B's newest lineage id as the
    cursor A still sees only its own (older or newer) rows."""
    a, b = seed.a, seed.b
    rows = repo.list_lineage(session, a.tenant, a.ontology, None, b.lineage, 50)
    assert [r["id"] for r in rows] == (
        [a.lineage] if context != "b" and a.lineage < b.lineage else []
    )
    assert repo.list_lineage(session, a.tenant, b.ontology, None, None, 50) == []


# ------------------------------------------------------------------ writes
def _state_of_b(owner_db: Database, seed: Seed) -> dict[str, Any]:
    b = seed.b
    with owner_db.session() as o:
        q = "SELECT {} FROM shifde.{} WHERE tenant_id = :t ORDER BY 1"
        out: dict[str, Any] = {}
        for label, cols, table in (
            (
                "policies",
                "id, object_type_key, subject_type, row_filter, row_version",
                "ontology_access_policies",
            ),
            (
                "attributes",
                "principal_id, attributes, row_version",
                "ontology_principal_attributes",
            ),
            (
                "mappings",
                "id, connection_id, relation_name, status, row_version",
                "ontology_source_mappings",
            ),
            ("info", "version_id, mappings_hash", "ontology_version_info"),
            ("lineage", "id, outcome", "ontology_query_lineage"),
        ):
            out[label] = [tuple(r) for r in o.execute(text(q.format(cols, table)), {"t": b.tenant})]
        return out


def test_a_cannot_change_bs_rows_through_the_repository(
    session: Session, context: str, seed: Seed, owner_db: Database
) -> None:
    a, b = seed.a, seed.b
    before = _state_of_b(owner_db, seed)
    t, unknown = a.tenant, uuid7()
    values = {
        "object_type_key": "x.taken",
        "subject_type": "user",
        "subject_id": uuid7(),
        "row_filter": "x",
    }
    assert repo.update_policy(session, t, b.ontology, b.policy, None, values, ["id"]) is None
    assert repo.update_policy(session, t, a.ontology, unknown, None, values, ["id"]) is None
    assert repo.delete_policy(session, t, b.ontology, b.policy) is False
    assert repo.delete_policy(session, t, a.ontology, b.policy) is False  # A's ontology, B's policy
    assert repo.delete_policy(session, t, unknown, unknown) is False
    # a write of B's principal key under A's tenant id never reaches B's row: expected version
    # given, no row of A's with that key
    assert repo.put_attributes(session, t, "user", b.principal, {"x": 1}, 1, uuid7()) is None
    build_repo.set_mapping_status(session, t, b.mapping_draft, "invalid")
    assert build_repo.delete_mapping(session, t, b.draft, "object_type", "x.order") is False
    build_repo.delete_mappings_of(session, t, b.draft)
    session.flush()
    assert _state_of_b(owner_db, seed) == before
    session.rollback()
    with owner_db.session() as o:
        assert (
            o.execute(
                text("SELECT count(*) FROM shifde.ontology_source_mappings WHERE tenant_id = :t"),
                {"t": b.tenant},
            ).scalar_one()
            == 2
        )


def test_a_own_writes_succeed_as_the_positive_control(
    app_db: Database, owner_db: Database, seed: Seed
) -> None:
    a = seed.a
    with app_session(app_db, a.tenant) as s:
        values = {
            "object_type_key": "x.new",
            "subject_type": "user",
            "subject_id": uuid7(),
            "row_filter": "r",
        }
        row = repo.update_policy(s, a.tenant, a.ontology, a.policy, None, values, ["id"])
        assert row is not None and row["object_type_key"] == "x.new" and row["row_version"] == 2
        assert repo.delete_policy(s, a.tenant, a.ontology, a.policy) is True
        stored = repo.put_attributes(s, a.tenant, "user", a.principal, {"k": 2}, 1, uuid7())
        assert stored is not None and stored["row_version"] == 2
        build_repo.set_mapping_status(s, a.tenant, a.mapping_draft, "ok")
        assert build_repo.delete_mapping(s, a.tenant, a.draft, "object_type", "x.order") is True
        s.commit()


def test_put_attributes_under_a_creates_a_row_of_a_and_leaves_bs_key(
    app_db: Database, owner_db: Database, seed: Seed
) -> None:
    """Primary key (tenant_id, principal_type, principal_id): A naming B's principal id gets a row
    of its own, never B's (no `ON CONFLICT`, no oracle)."""
    a, b = seed.a, seed.b
    with app_session(app_db, a.tenant) as s:
        row = repo.put_attributes(s, a.tenant, "user", b.principal, {"mine": 1}, None, uuid7())
        assert row is not None and row["attributes"] == {"mine": 1}
        s.commit()
    with owner_db.session() as o:
        stored = dict(
            o.execute(
                text(
                    "SELECT tenant_id, attributes FROM shifde.ontology_principal_attributes "
                    "WHERE principal_id = :p"
                ),
                {"p": b.principal},
            ).all()
        )
    assert stored == {a.tenant: {"mine": 1}, b.tenant: {"region": "b"}}


# ------------------------------------------------------------------ inserts naming B's rows
def _sqlstate(s: Session, write: Callable[[], object]) -> str:
    try:
        write()
        s.flush()
        return "ok"
    except NoResultFound:
        return "no-row"
    except DBAPIError as err:
        return str(getattr(err.orig, "sqlstate", None))
    finally:
        s.rollback()


def _mapping_insert_outcomes(s: Session, seed: Seed) -> dict[str, str]:
    a, b = seed.a, seed.b
    outcomes: dict[str, str] = {}
    for label, version in (("b-version", b.draft), ("unknown", uuid7())):
        outcomes[label] = _sqlstate(
            s,
            lambda v=version: build_repo.upsert_mapping(
                s, a.tenant, _mapping_values(a.tenant, v, a.connection, "x.customer")
            ),
        )
    return outcomes


def test_a_mapping_on_bs_version_gets_the_unknown_id_outcome(writer: Session, seed: Seed) -> None:
    outcomes = _mapping_insert_outcomes(writer, seed)
    assert outcomes["b-version"] == outcomes["unknown"] == "no-row"


def test_a_mapping_naming_bs_connection_gets_the_unknown_id_outcome(
    writer: Session, seed: Seed
) -> None:
    a, b = seed.a, seed.b
    outcomes = {
        label: _sqlstate(
            writer,
            lambda c=conn: build_repo.upsert_mapping(
                writer, a.tenant, _mapping_values(a.tenant, a.draft, c, "x.fresh")
            ),
        )
        for label, conn in (("b", b.connection), ("unknown", uuid7()))
    }
    assert outcomes == {"b": "23503", "unknown": "23503"}
    # control: A's own connection works
    assert (
        build_repo.upsert_mapping(
            writer, a.tenant, _mapping_values(a.tenant, a.draft, a.spare_connection, "x.fresh")
        )["target_key"]
        == "x.fresh"
    )


def test_a_mapping_upsert_on_the_same_target_never_touches_bs_row(
    app_db: Database, owner_db: Database, seed: Seed
) -> None:
    """B has a mapping for (B's draft, object_type, x.order); A upserts the same target on its
    own draft and then on B's draft: B's row stays byte-identical."""
    a, b = seed.a, seed.b
    before = _state_of_b(owner_db, seed)
    with app_session(app_db, a.tenant) as s:
        own = build_repo.upsert_mapping(
            s,
            a.tenant,
            _mapping_values(a.tenant, a.draft, a.spare_connection, "x.order", relation_name="mine"),
        )
        assert own["relation_name"] == "mine" and own["row_version"] == 2
        with pytest.raises(NoResultFound):
            build_repo.upsert_mapping(
                s, a.tenant, _mapping_values(a.tenant, b.draft, a.connection, "x.order")
            )
        s.rollback()
    assert _state_of_b(owner_db, seed) == before


def test_a_copy_of_bs_mappings_copies_nothing(
    writer: Session, seed: Seed, owner_db: Database
) -> None:
    a, b = seed.a, seed.b
    unknown = uuid7()
    assert build_repo.copy_mappings(writer, a.tenant, b.published, a.draft, uuid7()) == 0
    assert build_repo.copy_mappings(writer, a.tenant, unknown, a.draft, uuid7()) == 0
    # into B's draft: refused like an unknown target version, nothing written
    with pytest.raises(NoResultFound):
        build_repo.copy_mappings(writer, a.tenant, a.published, b.draft, uuid7())
    writer.rollback()
    with owner_db.session() as o:
        assert (
            o.execute(
                text("SELECT count(*) FROM shifde.ontology_source_mappings WHERE tenant_id = :t"),
                {"t": b.tenant},
            ).scalar_one()
            == 2
        )


def test_copy_mappings_into_own_draft_is_the_positive_control(app_db: Database, seed: Seed) -> None:
    a = seed.a
    with app_session(app_db, a.tenant) as s:
        assert build_repo.copy_mappings(s, a.tenant, a.published, a.draft, uuid7()) == 1
        keys = {m["target_key"] for m in build_repo.list_mappings(s, a.tenant, a.draft)}
        assert keys == {"x.customer", "x.order"}
        s.rollback()


def test_info_for_bs_version_is_the_unknown_id_outcome(
    writer: Session, seed: Seed, owner_db: Database
) -> None:
    a, b = seed.a, seed.b
    for version in (b.draft, uuid7()):  # B's draft has no info row; an unknown id has none
        build_repo.insert_ontology_info(writer, a.tenant, version, "h", {})
        writer.flush()
        assert build_repo.get_ontology_info(writer, a.tenant, version) is None
    # B's published version has an info row: the guard turns it into the same silent no-op
    build_repo.insert_ontology_info(writer, a.tenant, b.published, "stolen", {})
    writer.flush()
    writer.rollback()
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT tenant_id, mappings_hash FROM shifde.ontology_version_info ORDER BY 2")
        ).all()
    assert sorted(tuple(r) for r in rows) == sorted([(a.tenant, "hash-a"), (b.tenant, "hash-b")])


def test_a_policy_or_lineage_row_naming_bs_objects_is_refused(writer: Session, seed: Seed) -> None:
    a, b = seed.a, seed.b
    policy = {
        label: _sqlstate(
            writer,
            lambda o=onto: repo.insert_policy(
                writer, a.tenant, _policy_values(a.tenant, o, uuid7())
            ),
        )
        for label, onto in (
            ("b", b.ontology),
            ("unknown", uuid7()),
            ("version-as-ontology", b.published),
        )
    }
    assert set(policy.values()) == {"23503"}, policy
    lineage = {
        label: _sqlstate(
            writer,
            lambda v=version: repo.insert_lineage(
                writer, a.tenant, _lineage_values(a.tenant, a.ontology, v, a.connection)
            ),
        )
        for label, version in (("b", b.published), ("unknown", uuid7()))
    }
    assert lineage == {"b": "23503", "unknown": "23503"}
    # controls: A's own ontology and version
    assert repo.insert_policy(writer, a.tenant, _policy_values(a.tenant, a.ontology, uuid7()))
    repo.insert_lineage(
        writer, a.tenant, _lineage_values(a.tenant, a.ontology, a.published, a.connection)
    )
    writer.flush()


def test_a_session_for_a_cannot_write_bs_tenant_id(
    app_db: Database, owner_db: Database, seed: Seed
) -> None:
    a, b = seed.a, seed.b
    with app_session(app_db, a.tenant) as s:
        writes: list[Callable[[], object]] = [
            lambda: repo.insert_policy(s, b.tenant, _policy_values(b.tenant, b.ontology, uuid7())),
            lambda: repo.insert_lineage(
                s, b.tenant, _lineage_values(b.tenant, b.ontology, b.published, b.connection)
            ),
            lambda: repo.put_attributes(s, b.tenant, "user", uuid7(), {}, None, uuid7()),
            lambda: build_repo.insert_ontology_info(s, b.tenant, b.draft, "h", {}),
            lambda: build_repo.upsert_mapping(
                s, b.tenant, _mapping_values(b.tenant, b.draft, b.connection, "x.new")
            ),
        ]
        outcomes = [_sqlstate(s, write) for write in writes]
    # the guarded inserts (EXISTS on the client's own version, which row-level security hides in
    # A's context) write nothing and answer like an unknown id
    assert outcomes == ["42501", "42501", "42501", "ok", "no-row"]
    with owner_db.session() as o:
        assert (
            o.execute(
                text("SELECT count(*) FROM shifde.ontology_version_info WHERE tenant_id = :t"),
                {"t": b.tenant},
            ).scalar_one()
            == 1
        )


def test_without_a_client_context_nothing_of_a_is_readable_or_writable(
    app_db: Database, seed: Seed
) -> None:
    a = seed.a
    with app_session(app_db, None) as s:
        assert repo.list_policies(s, a.tenant, a.ontology) == []
        assert repo.get_lineage(s, a.tenant, a.lineage) is None
        assert build_repo.list_mappings(s, a.tenant, a.published) == []
        assert (
            _sqlstate(
                s,
                lambda: repo.insert_policy(
                    s, a.tenant, _policy_values(a.tenant, a.ontology, uuid7())
                ),
            )
            == "42501"
        )


# ------------------------------------------------------------------ no wait on B's uncommitted rows
@contextmanager
def _b_uncommitted(owner_db: Database, sql: str, params: dict[str, Any]) -> Iterator[None]:
    with owner_db.session() as o:
        o.execute(text(sql), params)  # not committed: holds its keys
        yield
        o.rollback()


def test_a_does_not_wait_on_bs_uncommitted_mapping_or_info_or_materialization(
    app_db: Database, owner_db: Database, seed: Seed
) -> None:
    a, b = seed.a, seed.b
    with owner_db.session() as o:
        spare, spare_artifact = uuid7(), uuid7()
        build_repo.insert_artifact(
            o,
            {
                "id": spare_artifact,
                "tenant_id": b.tenant,
                "kind": "ontologies",
                "workspace_id": None,
                "project_id": None,
                "key": "onto-spare",
                "name": "onto-spare",
                "description": None,
                "owner_principal_id": uuid7(),
                "visibility": "scope",
                "created_by": None,
            },
        )
        o.execute(
            text(
                "INSERT INTO shifde.artifact_versions (id, tenant_id, artifact_id, kind, "
                "version_no, state) VALUES (:v, :t, :a, 'ontologies', 1, 'draft')"
            ),
            {"v": spare, "t": b.tenant, "a": spare_artifact},
        )
        o.commit()
        o.execute(
            text(
                "INSERT INTO shifde.ontology_source_mappings (id, tenant_id, ontology_version_id, "
                "target_kind, target_key, connection_id, relation_name) VALUES (:i, :t, :v, "
                "'object_type', 'x.customer', :c, 'r')"
            ),
            {"i": uuid7(), "t": b.tenant, "v": spare, "c": b.connection},
        )
        o.execute(
            text(
                "INSERT INTO shifde.ontology_version_info (tenant_id, version_id, mappings_hash) "
                "VALUES (:t, :v, 'h')"
            ),
            {"t": b.tenant, "v": spare},
        )
        o.execute(
            text(
                "INSERT INTO shifde.ontology_materializations (id, tenant_id, "
                "ontology_version_id, object_type_key) VALUES (:i, :t, :v, 'x.customer')"
            ),
            {"i": uuid7(), "t": b.tenant, "v": spare},
        )
        with app_session(app_db, a.tenant) as s:
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            mapping = _sqlstate(
                s,
                lambda: build_repo.upsert_mapping(
                    s, a.tenant, _mapping_values(a.tenant, spare, a.connection, "x.customer")
                ),
            )
            assert mapping == "no-row"
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            build_repo.insert_ontology_info(s, a.tenant, spare, "h", {})
            s.flush()
            assert build_repo.get_ontology_info(s, a.tenant, spare) is None
            s.rollback()
            # raw inserts naming B's version and B's keys: the same answer as an unknown id,
            # and nothing waits
            for sql, state in (
                (
                    "INSERT INTO shifde.ontology_source_mappings (id, tenant_id, "
                    "ontology_version_id, target_kind, target_key, connection_id, relation_name) "
                    "VALUES (gen_random_uuid(), :t, :v, 'object_type', 'x.customer', :c, 'r')",
                    "23514",  # the guard trigger finds no such version for A's client
                ),
                (
                    "INSERT INTO shifde.ontology_version_info (tenant_id, version_id, "
                    "mappings_hash) VALUES (:t, :v, 'h')",
                    "23503",
                ),
                (
                    "INSERT INTO shifde.ontology_materializations (id, tenant_id, "
                    "ontology_version_id, object_type_key) "
                    "VALUES (gen_random_uuid(), :t, :v, 'x.customer')",
                    "42501",  # the app role has SELECT only (nothing writes it in the MVP)
                ),
            ):
                for version in (spare, uuid7()):  # B's uncommitted version, an unknown id
                    s.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    got = _sqlstate(
                        s,
                        lambda q=sql, v=version: s.execute(
                            text(q), {"t": a.tenant, "v": v, "c": a.connection}
                        ),
                    )
                    assert got == state, (sql, got)
        o.rollback()


def test_a_unknown_version_and_bs_version_fail_the_same_way_in_raw_inserts(
    writer: Session, seed: Seed
) -> None:
    a, b = seed.a, seed.b
    sql = (
        "INSERT INTO shifde.ontology_version_info (tenant_id, version_id, mappings_hash) "
        "VALUES (:t, :v, 'h')"
    )
    outcomes = {
        _sqlstate(writer, lambda v=v: writer.execute(text(sql), {"t": a.tenant, "v": v}))
        for v in (b.published, b.draft, uuid7())
    }
    assert outcomes == {"23503"}


# ------------------------------------------------------------------ database rules
def test_the_mappings_of_a_published_version_are_frozen_and_info_is_append_only(
    app_db: Database, seed: Seed
) -> None:
    a = seed.a
    with app_session(app_db, a.tenant) as s:
        states = [
            _sqlstate(
                s,
                lambda: build_repo.upsert_mapping(
                    s, a.tenant, _mapping_values(a.tenant, a.published, a.connection, "x.customer")
                ),
            ),
            _sqlstate(s, lambda: build_repo.delete_mappings_of(s, a.tenant, a.published)),
            _sqlstate(
                s,
                lambda: build_repo.delete_mapping(
                    s, a.tenant, a.published, "object_type", "x.customer"
                ),
            ),
            _sqlstate(
                s,
                lambda: s.execute(
                    text("DELETE FROM shifde.ontology_version_info WHERE tenant_id = :t"),
                    {"t": a.tenant},
                ),
            ),
        ]
    assert states[:3] == ["23514"] * 3
    assert states[3] == "42501"  # the app role has no DELETE grant on the append-only table


@pytest.mark.parametrize(
    "table",
    [
        "ontology_source_mappings",
        "ontology_version_info",
        "ontology_access_policies",
        "ontology_principal_attributes",
        "ontology_query_lineage",
        "ontology_materializations",
    ],
)
def test_raw_sql_as_the_app_role_sees_and_changes_only_its_own_rows(
    app_db: Database, owner_db: Database, seed: Seed, table: str
) -> None:
    with app_session(app_db, seed.a.tenant) as s:
        assert s.execute(text(f"SELECT count(*) FROM shifde.{table}")).scalar_one() >= 1
        assert (
            s.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :b"),
                {"b": seed.b.tenant},
            ).scalar_one()
            == 0
        )
        s.rollback()
    with owner_db.session() as o:
        assert (
            o.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :b"),
                {"b": seed.b.tenant},
            ).scalar_one()
            >= 1
        )
