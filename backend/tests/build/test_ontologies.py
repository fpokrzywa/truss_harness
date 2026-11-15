"""build part 3b: the ontology validator (`build.ontologies`) and the lookups it makes
(cross-client).

SETH tested it through the API (`test_p7_build.py`: the first half of
`test_capability_and_connection_state_are_checked_at_publish` calls `_connection_checks` directly
and is repeated here unchanged as `test_connection_state_and_capability_checks`; the rest of that
file needs the app and stays deferred, see carry_forward). These are new direct tests.

`validate_ontology`, `validate_mapping` and `check_one_mapping` never read the database: the
connection (`Env.connection`), its schema discovery (`Env.schema_discovery`), an action's tool
version (`Env.version`) and the principal's right to use it (`Env.permits`) are `Env` calls,
answered here by `RepoEnv` from the repositories with the caller's client id. Client B's object
must give the same issues (path, code, message) as an unknown id, and no column, table or id of B
may appear in the result; A's own object (control) validates. The lookup tests run as `shifde_app`
and as the schema owner (no row-level security: only the tenant id in the lookup confines A).

The statements a mapping later runs against the client's source are built by the adapter from the
validated mapping: the relation and every column must exist in the connection's own discovery
(`test_a_mapping_can_only_name_what_the_clients_own_discovery_lists`) and every filter value is a
bound parameter (`tests/build/test_ontology_expr.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from shifde.build.env import ConnectionInfo, Env, VersionRef
from shifde.build.issues import Issue, Report
from shifde.build.ontologies import (
    OntologyResult,
    _connection_checks,
    check_one_mapping,
    mappings_digest,
    validate_mapping,
    validate_ontology,
    validate_query_shape,
)
from shifde.core.ids import uuid7
from tests.build.conftest import EnvFactory, Side
from tests.build.env_support import Overriding, RepoEnv

CAPS = frozenset({"query.sql", "query.pushdown_join"})
CUSTOMER, ACCOUNT, REL = "cs.customer", "cs.account", "cs.customer_accounts"


class OntoEnv(RepoEnv):
    """`RepoEnv` whose database connections declare the capabilities of the postgres adapter."""

    def connection(self, connection_id: UUID) -> ConnectionInfo | None:
        info = super().connection(connection_id)
        return None if info is None else ConnectionInfo(**{**info.__dict__, "capabilities": CAPS})


def _onto(make_env: EnvFactory) -> EnvFactory:
    """`OntoEnv` for the stand-in; the service's own `Env` takes capabilities from the registry."""
    return OntoEnv if make_env is RepoEnv else make_env


def _prop(key: str, ptype: str, *, nullable: bool = False) -> dict[str, Any]:
    return {"key": key, "type": ptype, "nullable": nullable, "classification": "internal"}


def _spec(actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "namespaces": [{"key": "cs", "label": "Customer service"}],
        "object_types": [
            {
                "key": CUSTOMER,
                "namespace": "cs",
                "properties": [_prop("id", "integer"), _prop("name", "string")],
                "primary_key": ["id"],
                "display_property": "name",
            },
            {
                "key": ACCOUNT,
                "namespace": "cs",
                "properties": [_prop("id", "integer"), _prop("customer_id", "integer")],
                "primary_key": ["id"],
            },
        ],
        "relationships": [
            {"key": REL, "from": CUSTOMER, "to": ACCOUNT, "cardinality": "one_to_many"}
        ],
        "actions": actions or [],
    }


def _object_mapping(connection: UUID, table: str, **over: Any) -> dict[str, Any]:
    return {
        "target_kind": "object_type",
        "target_key": CUSTOMER,
        "connection_id": connection,
        "relation_schema": "public",
        "relation_name": table,
        "column_map": {"id": "id", "name": "name"},
        "key_map": None,
        "filters": [],
    } | over


def _account_mapping(connection: UUID, table: str) -> dict[str, Any]:
    return {
        "target_kind": "object_type",
        "target_key": ACCOUNT,
        "connection_id": connection,
        "relation_schema": "public",
        "relation_name": table,
        "column_map": {"id": "id", "customer_id": "id"},
        "key_map": None,
        "filters": [],
    }


def _relationship_mapping(connection: UUID, table: str) -> dict[str, Any]:
    return {
        "target_kind": "relationship",
        "target_key": REL,
        "connection_id": connection,
        "relation_schema": "public",
        "relation_name": table,
        "column_map": {},
        "key_map": {"from_column": "id", "to_column": "id"},
        "filters": [],
    }


def _all_mappings(side: Side) -> list[dict[str, Any]]:
    return [
        _object_mapping(side.connection, side.table),
        _account_mapping(side.connection, side.table),
        _relationship_mapping(side.connection, side.table),
    ]


def _sig(issues: list[Issue]) -> list[tuple[str, str, str]]:
    return [(i.path, i.code, i.message) for i in issues]


def _validate(
    env: Env, spec: dict[str, Any], mappings: list[dict[str, Any]]
) -> tuple[Report, OntologyResult]:
    report = Report()
    return report, validate_ontology(spec, mappings, env, report)


# ------------------------------------------------------------------ controls
def test_a_complete_ontology_with_the_callers_own_mappings_validates(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, _ = sides
    with opener(a.tenant) as s:
        env = _onto(make_env)(s, a.tenant)
        report, result = _validate(env, _spec(), _all_mappings(a))
        assert report.errors == [], _sig(report.errors)
        assert result.connection_ids == {a.connection}
        assert result.mappings_hash == mappings_digest(_all_mappings(a))
        check = check_one_mapping(_spec(), _object_mapping(a.connection, a.table), env)
        assert check.ok and check.issues == []
        assert {c["name"] for c in check.sample_columns} == {"id", "name"}
        s.rollback()


# ------------------------------------------------------------------ cross-client lookups
def test_a_mapping_on_bs_connection_gets_the_unknown_id_outcome(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = sides
    with opener(a.tenant) as s:
        env = _onto(make_env)(s, a.tenant)
        theirs = _object_mapping(b.connection, b.table)
        unknown = _object_mapping(uuid7(), b.table)
        their_report, their_result = _validate(env, _spec(), [theirs])
        unknown_report, _ = _validate(env, _spec(), [unknown])
        no_connection = [
            e for e in their_report.errors if e.message == "no such connection is available to you"
        ]
        assert len(no_connection) == 1 and no_connection[0].code == "mapping_invalid"
        assert _sig(their_report.errors) == _sig(unknown_report.errors)
        assert _sig(their_report.warnings) == _sig(unknown_report.warnings)
        # the mapping check endpoint: the same issues, and none of B's columns come back
        their_check = check_one_mapping(_spec(), theirs, env)
        unknown_check = check_one_mapping(_spec(), unknown, env)
        assert not their_check.ok and their_check.sample_columns == []
        assert their_check.issues == unknown_check.issues
        assert b.table not in repr(their_check) and "customers_b" not in repr(their_check.issues)
        # validate_mapping alone, with A's mapping naming B's connection
        report = Report()
        assert validate_mapping(theirs, _model(), {}, env, report) == []
        assert str(b.connection) not in repr(_sig(report.errors))
        assert their_result.connection_ids == {b.connection}  # the id A wrote, nothing of B's
        s.rollback()


def _model() -> dict[str, dict[str, Any]]:
    return {CUSTOMER: {"key": CUSTOMER, "properties": [], "primary_key": []}}


def test_a_mapping_reads_the_callers_own_schema_discovery_only(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = sides
    with opener(a.tenant) as s:
        env = _onto(make_env)(s, a.tenant)
        assert env.schema_discovery(a.connection) is not None
        assert env.schema_discovery(b.connection) is None
        assert env.schema_discovery(uuid7()) is None
        # A's connection, B's table name: not found, same message as a table nobody has
        other = check_one_mapping(_spec(), _object_mapping(a.connection, b.table), env)
        nobody = check_one_mapping(_spec(), _object_mapping(a.connection, "nobody_has_it"), env)
        assert not other.ok and other.sample_columns == []
        assert [i["message"] for i in other.issues] == [
            f"relation public.{b.table} not found",
        ]
        assert [i["code"] for i in other.issues] == [i["code"] for i in nobody.issues]
        s.rollback()


def test_a_mapping_can_only_name_what_the_clients_own_discovery_lists(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, _ = sides
    with opener(a.tenant) as s:
        env = _onto(make_env)(s, a.tenant)
        hostile_relations = [
            ("pg_catalog", "pg_authid"),
            ("shifde", "connections"),
            ("public", 'customers_a"; DROP TABLE x; --'),
            ('public"', a.table),
        ]
        for schema, table in hostile_relations:
            check = check_one_mapping(
                _spec(), _object_mapping(a.connection, table, relation_schema=schema), env
            )
            assert not check.ok and check.sample_columns == [], (schema, table)
            assert [i["code"] for i in check.issues] == ["mapping_invalid"], (schema, table)
        # a column that is not in the discovery (however it is spelled) is refused
        for column in ("secret", 'id"; DROP TABLE x; --', "id, tax_id", ""):
            check = check_one_mapping(
                _spec(),
                _object_mapping(a.connection, a.table, column_map={"id": "id", "name": column}),
                env,
            )
            assert not check.ok, column
        # a filter can name only mapped properties and is compiled to bound parameters
        for source in ("nope == 1", "id == name", "1 == 1; DROP TABLE x"):
            check = check_one_mapping(
                _spec(), _object_mapping(a.connection, a.table, filters=[source]), env
            )
            assert not check.ok, source
        good = check_one_mapping(
            _spec(),
            _object_mapping(a.connection, a.table, filters=["name != 'x\\'; DROP TABLE t; --'"]),
            env,
        )
        assert good.ok or all("filter 1" in i["message"] for i in good.issues)
        s.rollback()


# ------------------------------------------------------------------ actions
def _action(tool_version: Any, **over: Any) -> dict[str, Any]:
    return {
        "key": "cs.escalate",
        "object_type": CUSTOMER,
        "tool_version_id": str(tool_version),
        "param_mapping": {"item": "$object.name"},
    } | over


def test_an_action_naming_bs_tool_gets_the_unknown_id_outcome(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
    make_env: EnvFactory,
) -> None:
    a, b = sides
    with opener(a.tenant) as s:
        env = _onto(make_env)(s, a.tenant)
        report, result = _validate(env, _spec([_action(a.tool_version)]), _all_mappings(a))
        assert report.errors == [], _sig(report.errors)
        assert [(p[0].version_id, p[1]) for p in result.pins] == [(a.tool_version, "action_tool")]
        assert result.spec["actions"][0]["tool_version_id"] == str(a.tool_version)
        theirs, their_result = _validate(env, _spec([_action(b.tool_version)]), _all_mappings(a))
        unknown, _ = _validate(env, _spec([_action(uuid7())]), _all_mappings(a))
        assert [(e.path, e.code, e.message) for e in theirs.errors] == [
            (
                "actions[0].tool_version_id",
                "action_tool_invalid",
                "no such tool version is available to you",
            )
        ]
        assert _sig(theirs.errors) == _sig(unknown.errors)
        assert their_result.pins == []
        # an agent version is not a tool (A's own, so it is found, then refused by kind)
        wrong, _ = _validate(env, _spec([_action(a.agent_version)]), _all_mappings(a))
        assert [e.message for e in wrong.errors] == ["expected a tool version"]
        # B's agent version: the unknown-id outcome, not the wrong-kind one
        foreign, _ = _validate(env, _spec([_action(b.agent_version)]), _all_mappings(a))
        assert _sig(foreign.errors) == _sig(unknown.errors)
        s.rollback()


def test_an_action_on_a_forged_reference_to_bs_tool_is_not_permitted(
    sides: tuple[Side, Side],
    opener: Callable[[UUID], AbstractContextManager[Session]],
    owner_session: Session,
    make_env: EnvFactory,
) -> None:
    """If a lookup ever returned B's version, `permits` (artifact read with A's client id and
    the scope the reference claims) still refuses it; the version is read here as B."""
    a, b = sides
    foreign: VersionRef | None = RepoEnv(owner_session, b.tenant).version(b.tool_version)
    assert foreign is not None

    def leaky(inner: Env) -> Env:
        def version(version_id: UUID) -> VersionRef | None:
            return foreign if version_id == b.tool_version else inner.version(version_id)

        return Overriding(inner, version=version)

    with opener(a.tenant) as s:
        env = leaky(_onto(make_env)(s, a.tenant))
        report, result = _validate(env, _spec([_action(b.tool_version)]), _all_mappings(a))
        assert [(e.path, e.message) for e in report.errors] == [
            ("actions[0].tool_version_id", "you may not use this tool")
        ]
        assert result.pins == []
        s.rollback()


def test_action_rules(sides: tuple[Side, Side], owner_session: Session) -> None:
    a, _ = sides
    env = OntoEnv(owner_session, a.tenant)
    cases: list[tuple[dict[str, Any], str]] = [
        (_action(a.tool_version, object_type="cs.nope"), "unknown object type 'cs.nope'"),
        (
            _action(a.tool_version, param_mapping={"item": "$object.zip"}),
            "'$object.zip' does not name a property of cs.customer",
        ),
        (
            _action(a.tool_version, param_mapping={"bogus": "x", "item": "x"}),
            "the tool declares no such parameter",
        ),
        (_action(a.tool_version, param_mapping=["x"]), "param_mapping maps tool parameters to "),
    ]
    for action, message in cases:
        report, _ = _validate(env, _spec([action]), _all_mappings(a))
        assert any(
            e.code == "action_tool_invalid" and e.message.startswith(message) for e in report.errors
        ), (message, _sig(report.errors))
    unbound, _ = _validate(
        env, _spec([_action(a.tool_version, param_mapping={})]), _all_mappings(a)
    )
    assert [w.code for w in unbound.warnings] == ["action_param_unbound"]
    twice, _ = _validate(
        env, _spec([_action(a.tool_version), _action(a.tool_version)]), _all_mappings(a)
    )
    assert ("actions[1].key", "duplicate_key") in {(e.path, e.code) for e in twice.errors}


# ------------------------------------------------------------------ logical model and publishing
def test_the_logical_model_rules(sides: tuple[Side, Side], owner_session: Session) -> None:
    a, _ = sides
    env = OntoEnv(owner_session, a.tenant)

    def codes(spec: dict[str, Any]) -> set[tuple[str, str]]:
        report, _ = _validate(env, spec, _all_mappings(a))
        return {(e.path, e.code) for e in report.errors}

    assert codes(_spec()) == set()
    spec = _spec()
    spec["object_types"][0]["namespace"] = "nope"
    assert ("object_types[0].namespace", "unknown_namespace") in codes(spec)
    spec = _spec()
    spec["object_types"][0]["primary_key"] = ["zip"]
    assert ("object_types[0].primary_key", "invalid_primary_key") in codes(spec)
    spec = _spec()
    spec["object_types"][0]["properties"][0]["nullable"] = True
    assert ("object_types[0].primary_key", "invalid_primary_key") in codes(spec)
    spec = _spec()
    spec["object_types"][0]["properties"].append(_prop("and", "string"))
    assert ("object_types[0].properties[2].key", "invalid_value") in codes(spec)
    spec = _spec()
    spec["object_types"][0]["properties"].append(_prop("name", "string"))
    assert ("object_types[0].properties[2].key", "duplicate_key") in codes(spec)
    spec = _spec()
    spec["object_types"][0]["properties"][1]["type"] = "blob"
    assert ("object_types[0].properties[1].type", "invalid_type") in codes(spec)
    spec = _spec()
    spec["relationships"][0]["to"] = "cs.nope"
    assert ("relationships[0].to", "dangling_relationship") in codes(spec)
    spec = _spec()
    spec["relationships"][0]["cardinality"] = "some"
    assert ("relationships[0].cardinality", "invalid_cardinality") in codes(spec)
    spec = _spec()
    spec["object_types"].append(dict(spec["object_types"][0]))
    assert ("object_types[2].key", "duplicate_key") in codes(spec)
    spec = _spec()
    spec["surprise"] = 1
    report, _ = _validate(env, spec, _all_mappings(a))
    assert ("surprise", "unknown_field") in {(w.path, w.code) for w in report.warnings}
    # a type or relationship without a mapping, a mapping for a type the model does not have
    report, _ = _validate(env, _spec(), [])
    assert {(e.path, e.code) for e in report.errors} == {
        ("object_types[0]", "unmapped_object_type"),
        ("object_types[1]", "unmapped_object_type"),
        ("relationships[0]", "unmapped_relationship"),
    }
    stray = _object_mapping(a.connection, a.table, target_key="cs.nope")
    check = check_one_mapping(_spec(), stray, env)
    assert not check.ok and check.issues[0]["message"] == "the ontology has no such object type"


def test_mapping_checks_against_discovery(sides: tuple[Side, Side], owner_session: Session) -> None:
    a, _ = sides
    env = OntoEnv(owner_session, a.tenant)

    def issues(mapping: dict[str, Any]) -> list[tuple[str, str]]:
        check = check_one_mapping(_spec(), mapping, env)
        return [(i["code"], i["message"]) for i in check.issues if i["severity"] == "error"]

    assert issues(_object_mapping(a.connection, a.table)) == []
    assert issues(_object_mapping(a.connection, a.table, column_map={"id": "id"})) == []
    assert issues(_object_mapping(a.connection, a.table, column_map={"name": "name"})) == [
        ("mapping_invalid", "primary key property 'id' is not mapped")
    ]
    assert issues(
        _object_mapping(a.connection, a.table, column_map={"id": "name", "name": "id"})
    ) == [
        ("type_incompatible", "property 'id' (integer) cannot be backed by 'name' (text)"),
        ("type_incompatible", "property 'name' (string) cannot be backed by 'id' (integer)"),
    ]
    assert issues(_object_mapping(a.connection, a.table, column_map={"id": "id", "zip": "id"})) == [
        ("mapping_invalid", "'zip' is not a property of cs.customer")
    ]
    assert issues(_relationship_mapping(a.connection, a.table)) == []
    broken = _relationship_mapping(a.connection, a.table)
    broken["key_map"] = {"from_column": "id"}
    assert issues(broken) == [("mapping_invalid", "key_map needs from_column and to_column")]
    broken["key_map"] = {"from_column": "id", "to_column": "id", "relation": {"name": "other"}}
    assert issues(broken)[0][1].startswith("key_map.relation must repeat")


def test_connection_state_and_capability_checks() -> None:
    """The first half of SETH `test_p7_build.py::test_capability_and_connection_state_are_checked_at_publish`,
    assertions unchanged (the end-to-end half needs the app)."""
    report = Report()
    info = ConnectionInfo(
        uuid4(),
        "database",
        "postgres",
        "active",
        "read",
        True,
        True,
        frozenset({"discover.schema"}),
    )
    assert _connection_checks(info, "object_type", CUSTOMER, report, True) is False
    assert report.errors[0].code == "capability_missing" and report.errors[0].node_id == CUSTOMER
    report = Report()
    inactive = ConnectionInfo(
        uuid4(), "database", "postgres", "disabled", "read", True, True, frozenset({"query.sql"})
    )
    assert _connection_checks(inactive, "relationship", REL, report, True) is False
    assert report.errors[0].code == "mapping_invalid" and report.errors[0].key == REL
    report = Report()
    rest = ConnectionInfo(
        uuid4(), "rest", "rest.http", "active", "read", True, True, frozenset({"rest.call"})
    )
    assert _connection_checks(rest, "object_type", CUSTOMER, report, True) is False


def test_a_connection_the_publisher_may_not_use_is_refused_unless_use_is_not_checked() -> None:
    info = ConnectionInfo(
        uuid4(), "database", "postgres", "active", "read", False, True, frozenset({"query.sql"})
    )
    report = Report()
    assert _connection_checks(info, "object_type", CUSTOMER, report, True) is False
    assert report.errors[0].message == "you may not use this connection"
    report = Report()
    assert _connection_checks(info, "object_type", CUSTOMER, report, False) is True
    # a relationship over an adapter without join pushdown is a warning, not an error
    report = Report()
    assert _connection_checks(info, "relationship", REL, report, False) is True
    assert [w.code for w in report.warnings] == ["keyed_fetch"]


def test_the_mappings_digest_is_order_independent_and_covers_every_field() -> None:
    cid = uuid4()
    one = _object_mapping(cid, "t")
    two = _account_mapping(cid, "t")
    assert mappings_digest([one, two]) == mappings_digest([two, one])
    assert mappings_digest([one]) != mappings_digest([one, two])
    for change in (
        {"connection_id": uuid4()},
        {"relation_name": "u"},
        {"relation_schema": "other"},
        {"column_map": {"id": "id"}},
        {"filters": ["id > 1"]},
        {"key_map": {"from_column": "a", "to_column": "b"}},
    ):
        assert mappings_digest([one | change]) != mappings_digest([one]), change


# ------------------------------------------------------------------ query shape (pure)
QUERY_SPEC = _spec()


def _shape(query: Any, *, max_depth: int = 3) -> list[tuple[str, str]]:
    report = Report()
    validate_query_shape(query, QUERY_SPEC, report, "q", "node", max_depth=max_depth)
    return [(e.path, e.code) for e in report.errors]


def test_query_shape_accepts_a_declared_query() -> None:
    query = {
        "object_type": CUSTOMER,
        "select": ["id", "name"],
        "filter": "row.id > 1",
        "limit": 10,
        "traverse": [{"relationship": REL, "select": ["customer_id"]}],
    }
    assert _shape(query) == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("x", [("q", "invalid_config")]),
        ({"object_type": "cs.other", "select": ["id"]}, [("q.object_type", "invalid_config")]),
        ({"object_type": CUSTOMER, "select": ["zip"]}, [("q.select", "invalid_config")]),
        ({"object_type": CUSTOMER, "select": []}, [("q.select", "invalid_config")]),
        ({"object_type": CUSTOMER, "select": ["id"], "limit": 0}, [("q.limit", "invalid_config")]),
        (
            {"object_type": CUSTOMER, "select": ["id"], "limit": 1001},
            [("q.limit", "invalid_config")],
        ),
        (
            {"object_type": CUSTOMER, "select": ["id"], "limit": True},
            [("q.limit", "invalid_config")],
        ),
        (
            {"object_type": CUSTOMER, "select": ["id"], "filter": 5},
            [("q.filter", "invalid_config")],
        ),
        (
            {"object_type": CUSTOMER, "select": ["id"], "filter": "id =="},
            [("q.filter", "expression_invalid")],
        ),
        (
            {
                "object_type": CUSTOMER,
                "select": ["id"],
                "traverse": [{"relationship": "cs.nope", "select": ["id"]}],
            },
            [("q.traverse[0]", "invalid_config")],
        ),
        (
            {
                "object_type": CUSTOMER,
                "select": ["id"],
                "traverse": [{"relationship": REL, "direction": "reverse", "select": ["id"]}],
            },
            [("q.traverse[0]", "invalid_config")],
        ),
        (
            {
                "object_type": CUSTOMER,
                "select": ["id"],
                "traverse": [{"relationship": REL, "select": ["name"]}],
            },
            [("q.traverse[0].select", "invalid_config")],
        ),
    ],
)
def test_query_shape_refuses_what_the_spec_does_not_declare(
    query: Any, expected: list[tuple[str, str]]
) -> None:
    assert _shape(query) == expected


def test_query_shape_limits_traversal_depth() -> None:
    hop = {"relationship": REL, "select": ["id"]}
    query = {"object_type": CUSTOMER, "select": ["id"], "traverse": [hop]}
    assert _shape(query, max_depth=1) == []
    assert _shape(query, max_depth=0) == [("q.traverse[0]", "invalid_config")]
