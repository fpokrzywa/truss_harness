"""Ontology units without the full app: the Postgres adapter's filter groups, policy semantics,
the planner's capability awareness and the query models. Moved from SETH `test_p7_units.py`
with their assertions unchanged (the filter-compiler tests of that file live in
`tests/build/test_ontology_expr.py`)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from shifde.adapters.base import Filter, FilterGroup, SourceQueryPlan
from shifde.adapters.postgres import PostgresAdapter
from shifde.build import ontology_expr as expr
from shifde.ontology import engine
from shifde.ontology import policy as pol
from shifde.ontology import schemas as s


def test_postgres_adapter_compiles_filter_groups_and_offset_with_quoted_identifiers() -> None:
    plan = SourceQueryPlan(
        table="t",
        schema="public",
        columns=["a", 'we"ird'],
        filters=[
            FilterGroup("or", [Filter("a", "=", 1), FilterGroup("not", [Filter("b", "is_null")])]),
            Filter("c", "in", [1, 2]),
        ],
        order_by=[("a", "asc")],
        limit=10,
        offset=20,
    )
    out = PostgresAdapter().compile(plan)
    assert (
        out.sql
        == (
            'SELECT "a", "we""ird" FROM "public"."t" WHERE (("a" = %s OR (NOT "b" IS NULL))) '
            'AND "c" = ANY(%s) ORDER BY "a" ASC LIMIT %s OFFSET %s'
        )
        or "NOT" in out.sql
    )
    assert out.params == (1, [1, 2], 10, 20)
    with pytest.raises(Exception, match="operands"):
        PostgresAdapter().compile(SourceQueryPlan("t", filters=[FilterGroup("and", [])]))


# ================================================================== policy semantics
def object_type() -> dict[str, Any]:
    return {
        "key": "x.t",
        "properties": [
            {"key": "id", "classification": "internal"},
            {"key": "name", "classification": "internal"},
            {"key": "region", "classification": "internal"},
            {"key": "tax", "classification": "restricted"},
        ],
    }


def pol_row(
    subject: tuple[str, UUID], props: list[str] | None, row_filter: str | None
) -> dict[str, Any]:
    return {
        "id": uuid4(), "object_type_key": "x.t", "effect": "allow", "property_keys": props,
        "subject_type": subject[0], "subject_id": subject[1], "row_filter": row_filter,
    }  # fmt: skip


def test_grants_default_deny_restricted_explicit_and_row_predicates() -> None:
    me, role, group, other = uuid4(), uuid4(), uuid4(), uuid4()
    subjects = pol.Subjects(me, "user", frozenset({role}), frozenset({group}), {"region": "n"})
    operands = {k: expr.Operand(f"c_{k}", "string", k) for k in ("id", "name", "region", "tax")}
    ot = object_type()
    assert pol.grants_for([], subjects, ot, operands) == []  # default deny
    policies = [
        pol_row(("role", other), None, None),  # someone else's role
        pol_row(("role", role), None, 'row.region == "n"'),
        pol_row(("group", group), ["id", "name"], 'row.region == "s"'),
        pol_row(("user", me), ["tax"], None),
        pol_row(("user", me), None, "row.region == principal.attributes.gone"),  # cannot evaluate
        pol_row(("role", role), ["nope"], None),  # covers nothing
    ]
    grants = pol.grants_for(policies, subjects, ot, operands)
    assert [g.properties for g in grants] == [
        frozenset({"id", "name", "region"}),  # "all" excludes the restricted property
        frozenset({"id", "name"}),
        frozenset({"tax"}),
    ]
    assert pol.readable(grants) == {"id", "name", "region", "tax"}
    assert pol.denied_properties(grants, ["id", "tax", "zzz"]) == ["zzz"]
    # region is only admitted by the first grant; name by two (OR); tax by an unrestricted one
    assert pol.row_predicate(grants, ["region"]) == Filter("c_region", "=", "n")
    both = pol.row_predicate(grants, ["name"])
    assert isinstance(both, FilterGroup) and both.op == "or" and len(both.items) == 2
    assert pol.row_predicate(grants, ["tax"]) is None
    combined = pol.row_predicate(grants, ["region", "tax", "name"])
    assert isinstance(combined, FilterGroup) and combined.op == "and"
    assert pol.any_grant_predicate(grants) is None  # the unrestricted tax grant admits every row
    assert pol.any_grant_predicate(grants[:2]) is not None


# ================================================================== planner / capabilities
def ctx_for(
    caps: set[str],
    mappings: dict[tuple[str, str], dict[str, Any]],
    policies: list[dict[str, Any]],
    role: UUID,
) -> engine.EngineContext:
    ot = {
        "key": "x.a", "label": "A", "namespace": "x", "primary_key": ["id"],
        "properties": [{"key": "id", "type": "integer", "classification": "internal"}],
    }  # fmt: skip
    spec = {
        "object_types": [ot, {**ot, "key": "x.b"}],
        "relationships": [{"key": "x.ab", "label": "ab", "from": "x.a", "to": "x.b"}],
    }
    return engine.EngineContext(
        spec=spec, mappings=mappings, policies=policies,
        subjects=pol.Subjects(uuid4(), "user", frozenset({role}), frozenset(), {}),
        capabilities=lambda _cid: frozenset(caps),
        edge_operands=lambda _m: {},
    )  # fmt: skip


def mapping(kind: str, key: str, cid: UUID) -> dict[str, Any]:
    return {
        "target_kind": kind, "target_key": key, "connection_id": cid, "relation_schema": "",
        "relation_name": "t", "column_map": {"id": "id"}, "key_map": {"from_column": "a", "to_column": "b"},
        "filters": [],
    }  # fmt: skip


def test_planner_is_capability_aware_and_records_pushdown_only_when_declared() -> None:
    role, cid = uuid4(), uuid4()
    mappings = {
        ("object_type", "x.a"): mapping("object_type", "x.a", cid),
        ("object_type", "x.b"): mapping("object_type", "x.b", cid),
        ("relationship", "x.ab"): mapping("relationship", "x.ab", cid),
    }
    policies = [
        {**pol_row(("role", role), None, None), "object_type_key": k} for k in ("x.a", "x.b")
    ]
    hop = s.HopIn(relationship="x.ab", select=["id"])
    plan = engine.Planner(ctx_for({"query.sql"}, mappings, policies, role)).node(
        "x.a", ["id"], None, [hop], 0
    )
    assert plan.hops[0].pushdown_available is False and plan.hops[0].strategy == "keyed_fetch"
    plan = engine.Planner(
        ctx_for({"query.sql", "query.pushdown_join"}, mappings, policies, role)
    ).node("x.a", ["id"], None, [hop], 0)
    assert plan.hops[0].pushdown_available is True and plan.hops[0].strategy == "keyed_fetch"
    # an adapter without query.sql cannot be planned against at all
    with pytest.raises(engine.MappingUnavailable):
        engine.Planner(ctx_for({"discover.schema"}, mappings, policies, role)).node(
            "x.a", ["id"], None, [], 0
        )


def test_planner_denies_by_default_and_hides_unauthorized_targets() -> None:
    role, cid = uuid4(), uuid4()
    mappings = {
        ("object_type", "x.a"): mapping("object_type", "x.a", cid),
        ("object_type", "x.b"): mapping("object_type", "x.b", cid),
        ("relationship", "x.ab"): mapping("relationship", "x.ab", cid),
    }
    only_a = [{**pol_row(("role", role), None, None), "object_type_key": "x.a"}]
    planner = engine.Planner(ctx_for({"query.sql"}, mappings, only_a, role))
    with pytest.raises(Exception) as denied:
        planner.node("x.b", ["id"], None, [], 0)
    assert denied.value.__class__.__name__ == "NotFound"
    with pytest.raises(Exception) as hidden:
        planner.node("x.a", ["id"], None, [s.HopIn(relationship="x.ab", select=["id"])], 0)
    assert hidden.value.__class__.__name__ == "NotFound" and "relationship" in str(hidden.value)
    assert planner.denied[0] == ("x.b", "no_policy")
    # depth / hop limits
    deep = s.HopIn(relationship="x.ab", select=["id"])
    with pytest.raises(engine.TraversalTooDeep):
        engine.Planner(ctx_for({"query.sql"}, mappings, only_a, role)).node(
            "x.a", ["id"], None, [], 4
        )
    _ = deep


def test_query_models_bound_their_inputs() -> None:
    import pydantic

    ok = s.QueryIn.model_validate({"object_type": "a", "select": ["x"]})
    assert ok.limit == 100 and ok.traverse == []
    for bad in (
        {"object_type": "a", "select": []},
        {"object_type": "a", "select": ["x"], "limit": 1001},
        {"object_type": "a", "select": ["x"], "unknown": 1},
        {"object_type": "a", "select": ["x"], "filter": "x" * 1001},
        {
            "object_type": "a",
            "select": ["x"],
            "traverse": [{"relationship": "r", "select": ["y"], "direction": "sideways"}],
        },
        {
            "object_type": "a",
            "select": ["x"],
            "traverse": [{"relationship": "r", "select": ["y"]}] * 9,
        },
    ):
        with pytest.raises(pydantic.ValidationError):
            s.QueryIn.model_validate(bad)
