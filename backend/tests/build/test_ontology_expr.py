"""The ontology filter-expression compiler (`build.ontology_expr`), moved from SETH
`test_p7_units.py` with assertions unchanged. The policy, planner, query-model and sample-ontology
tests of that file need `shifde.ontology` / the app and move with them (carry_forward)."""

from __future__ import annotations

from typing import Any

import pytest

from shifde.adapters.base import Filter, FilterGroup, SourceQueryPlan
from shifde.adapters.postgres import PostgresAdapter
from shifde.build import ontology_expr as expr

COLS = {
    "id": expr.Operand("c_id", "integer", "id"),
    "name": expr.Operand("c_name", "string", "name"),
    "region": expr.Operand("c_region", "string", "region"),
    "active": expr.Operand("c_active", "boolean", "active"),
    "when": expr.Operand("c_when", "datetime", "when"),
    "day": expr.Operand("c_day", "date", "day"),
    "doc": expr.Operand("c_doc", "json", "doc"),
    "score": expr.Operand("c_score", "number", "score"),
}


def compile_(source: str, **kw: Any) -> Any:
    return expr.compile_filter(source, expr.bare_resolver(COLS), **kw)


# ================================================================== filter compiler
def test_comparisons_compile_to_bound_filters_with_the_column_first() -> None:
    assert compile_('region == "north"') == Filter("c_region", "=", "north")
    assert compile_("5 < id") == Filter("c_id", ">", 5)  # a value on the left is flipped
    assert compile_("id >= 2 and name != 'x'") == FilterGroup(
        "and", [Filter("c_id", ">=", 2), Filter("c_name", "!=", "x")]
    )
    assert compile_("not (id == 1 or id == 2)") == FilterGroup(
        "not", [FilterGroup("or", [Filter("c_id", "=", 1), Filter("c_id", "=", 2)])]
    )
    assert compile_("name == null") == Filter("c_name", "is_null")
    assert compile_("name != null") == FilterGroup("not", [Filter("c_name", "is_null")])
    assert compile_("active") == Filter("c_active", "=", True)
    assert compile_("row.id == 3") == Filter("c_id", "=", 3)  # a leading `row.` is accepted
    assert compile_("score > 1.5") == Filter("c_score", ">", 1.5)
    got = compile_('when >= "2025-03-01T10:00:00Z"')
    assert isinstance(got, Filter) and got.value.tzinfo is not None
    assert compile_('day == "2025-03-01"').value.isoformat() == "2025-03-01"  # type: ignore[union-attr]  # the compiled node is a Filter here (asserted by the line above)
    assert compile_("region == params.r", consts={"r": "south"}) == Filter("c_region", "=", "south")


@pytest.mark.parametrize(
    "source",
    [
        "id == name",  # column to column
        "1 == 1",  # no column
        "id == '5'",  # wrong literal type
        "name == 5",
        "id == true",
        "doc == 'x'",  # JSON only against null
        "active > true",
        "name in 'abc'",  # string containment
        "name in name",
        "id",  # a non-boolean property alone
        "unknown == 1",  # not a mapped property
        "nope.id == 1",
        "row.row.id == 1",
        "id == 1; DROP TABLE x",
        "id == (1",
        "id = 1",
        "name.length == 3",
        "f(id) == 1",
        'when == "yesterday"',
        'day == "2025-13-45"',
        "id == params.x",  # constants are not available here
    ],
)
def test_unsupported_or_unsafe_expressions_are_refused(source: str) -> None:
    with pytest.raises(expr.FilterError):
        compile_(source)


@pytest.mark.parametrize("value", ["x'; DROP TABLE t; --", '" OR "1"="1', "100%d%%", "a\\'b", "🙂"])
def test_values_are_only_ever_bound_never_spliced(value: str) -> None:
    import json

    node = compile_(f"name == {json.dumps(value, ensure_ascii=False)}")
    assert node == Filter("c_name", "=", value)
    compiled = PostgresAdapter().compile(SourceQueryPlan("t", None, ["c_name"], [node]))
    assert value not in compiled.sql and compiled.params == (value,)


def test_principal_values_fail_closed_and_lists_use_in() -> None:
    resolve = expr.row_resolver(COLS)
    ctx = {
        "id": "u",
        "type": "user",
        "attributes": {"region": "north", "regions": ["a", "b"], "n": 3},
    }
    f = expr.compile_filter("row.region == principal.attributes.region", resolve, principal=ctx)
    assert f == Filter("c_region", "=", "north")
    f = expr.compile_filter("row.region in principal.attributes.regions", resolve, principal=ctx)
    assert f == Filter("c_region", "in", ["a", "b"])
    f = expr.compile_filter("row.id == principal.attributes.n", resolve, principal=ctx)
    assert f == Filter("c_id", "=", 3)
    for missing in (
        "row.region == principal.attributes.zone",
        "not (row.region == principal.attributes.zone)",  # `not` must not turn "unknown" into true
    ):
        with pytest.raises(expr.PrincipalValueMissing):
            expr.compile_filter(missing, resolve, principal=ctx)
    with pytest.raises(expr.FilterError):  # a whole sub-document is not a value
        expr.compile_filter("row.region == principal.attributes", resolve, principal=ctx)
    with pytest.raises(expr.FilterError):  # bare names are not policy fields
        expr.compile_filter("region == principal.attributes.region", resolve, principal=ctx)
    with pytest.raises(expr.FilterError):  # a list needs `in`
        expr.compile_filter("row.region == principal.attributes.regions", resolve, principal=ctx)


def test_type_compatibility_matrix() -> None:
    ok = [
        ("string", "character varying(20)"), ("string", "uuid"), ("integer", "bigint"),
        ("number", "numeric(12,2)"), ("number", "integer"), ("boolean", "boolean"),
        ("date", "date"), ("datetime", "timestamp with time zone"), ("json", "jsonb"),
    ]  # fmt: skip
    bad = [
        ("integer", "text"), ("integer", "numeric"), ("string", "integer"), ("boolean", "integer"),
        ("date", "timestamp with time zone"), ("json", "text"), ("string", "USER-DEFINED"),
    ]  # fmt: skip
    assert all(expr.type_compatible(p, d) for p, d in ok)
    assert not any(expr.type_compatible(p, d) for p, d in bad)
    assert (
        expr.ptype_of_db_type("timestamptz") == "datetime"
        and expr.ptype_of_db_type("point") is None
    )
