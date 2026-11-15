"""Ontology engine: exact numeric keys (REQ F-04, DECISIONS B-2003).

SETH converted a numeric string from an adapter (`numeric`/`bigint` columns arrive as text) with
`float()`: two different keys above 2**53 became one value, so a relationship led to the wrong
object. Values stay exact now: integer literals are `int`, other numbers are `float` only when
that prints back to the same digits, else `Decimal`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from shifde.adapters.base import Filter, SourceQueryPlan
from shifde.ontology import engine

BIG = 2**53 + 1  # 9007199254740993: float(BIG) == float(BIG - 1)


def test_the_float_round_trip_this_replaces_loses_the_key() -> None:
    assert float(str(BIG)) == float(str(BIG - 1))  # the defect: two keys, one value
    assert engine.normalize_value("number", str(BIG)) != engine.normalize_value(
        "number", str(BIG - 1)
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (str(BIG), BIG),
        (str(BIG - 1), BIG - 1),
        ("-9007199254740993", -BIG),
        ("+42", 42),
        ("0", 0),
        ("1.5", 1.5),
        ("0.1", 0.1),
        ("5.0", 5.0),
        ("1e3", 1000.0),
        (" 7 ", 7),
        ("12345678901234567.25", Decimal("12345678901234567.25")),
        ("0.1000000000000000055511151231257827", Decimal("0.1000000000000000055511151231257827")),
        ("not a number", "not a number"),
        ("", ""),
    ],
)
def test_numeric_text_is_converted_without_losing_digits(text: str, expected: Any) -> None:
    got = engine.normalize_value("number", text)
    assert got == expected and type(got) is type(expected)


def test_other_values_pass_through() -> None:
    assert engine.normalize_value("number", None) is None
    assert engine.normalize_value("number", BIG) == BIG
    assert engine.normalize_value("number", Decimal("1.25")) == Decimal("1.25")
    assert engine.normalize_value("string", "9007199254740993") == "9007199254740993"
    assert engine.normalize_value("integer", "9007199254740993") == "9007199254740993"
    assert engine.normalize_value("number", "nan") != engine.normalize_value("number", "nan")


def _never(*_: Any) -> Any:
    raise AssertionError("the fake source runs no statement")


class FakeSource(engine.Source):
    """Tables as text rows, like the adapter hands numeric columns over; `in` filters match on
    the printed value, so a float that lost digits finds nothing (or the wrong row)."""

    def __init__(self, tables: dict[str, tuple[list[str], list[list[str]]]]) -> None:
        super().__init__(_never, _never)
        self.tables = tables
        self.plans: list[SourceQueryPlan] = []

    def run(
        self, connection_id: UUID, plan: SourceQueryPlan
    ) -> tuple[list[str], list[list[Any]], bool]:
        self.plans.append(plan)
        names, rows = self.tables[plan.table]
        for f in plan.filters:
            if isinstance(f, Filter) and f.op == "in":
                column = names.index(f.column)
                wanted = {str(v) for v in f.value}
                rows = [r for r in rows if str(r[column]) in wanted]
        return names, [list(r) for r in rows], False


def _node(table: str, select: list[str]) -> engine.NodePlan:
    return engine.NodePlan(
        object_type=table,
        connection_id=uuid4(),
        schema=None,
        table=table,
        select=select,
        pk=["id"],
        columns={k: k for k in dict.fromkeys([*select, "id"])},
        ptypes={"id": "number", "name": "string"},
        filters=[],
    )


def test_a_relationship_leads_to_the_object_with_the_same_exact_key() -> None:
    even, odd = str(BIG - 1), str(BIG)
    source = FakeSource(
        {
            "customer": (["id", "name"], [[even, "even customer"], [odd, "odd customer"]]),
            "edge": (["customer_id", "order_id"], [[even, even], [odd, odd]]),
            "orders": (["id", "name"], [[even, "even order"], [odd, "odd order"]]),
        }
    )
    root = _node("customer", ["id", "name"])
    root.hops.append(
        engine.HopPlan(
            relationship="placed",
            label="placed",
            direction="forward",
            edge_connection_id=root.connection_id,
            edge_schema=None,
            edge_table="edge",
            start_column="customer_id",
            end_column="order_id",
            edge_filters=[],
            target=_node("orders", ["id", "name"]),
        )
    )
    result = engine.Executor(source).run(root, limit=10, offset=0)
    by_name = {r["name"]: r for r in result.rows}
    assert by_name["odd customer"]["id"] == BIG and by_name["even customer"]["id"] == BIG - 1
    odd_links = by_name["odd customer"][engine.LINKS]["placed"]
    even_links = by_name["even customer"][engine.LINKS]["placed"]
    assert [(link["id"], link["name"]) for link in odd_links] == [(BIG, "odd order")]
    assert [(link["id"], link["name"]) for link in even_links] == [(BIG - 1, "even order")]
    # the keys reach the source as exact integers, never as floats
    sent = [f.value for p in source.plans for f in p.filters if isinstance(f, Filter)]
    assert sorted(v for chunk in sent for v in chunk) == sorted([BIG - 1, BIG] * 2)
    assert all(type(v) is int for chunk in sent for v in chunk)


def test_a_filter_on_a_key_above_2_53_reaches_the_driver_as_an_exact_bound_integer() -> None:
    from shifde.adapters.postgres import PostgresAdapter
    from shifde.build import ontology_expr as expr

    operands = {"id": expr.Operand("id", "number", "id")}
    node = expr.compile_filter(f"id == {BIG}", expr.bare_resolver(operands))
    assert isinstance(node, Filter) and node == Filter("id", "=", BIG)
    assert type(node.value) is int
    compiled = PostgresAdapter().compile(
        SourceQueryPlan(table="t", columns=["id"], filters=[node], limit=1)
    )
    assert str(BIG) not in compiled.sql and compiled.params[0] == BIG
    assert type(compiled.params[0]) is int
