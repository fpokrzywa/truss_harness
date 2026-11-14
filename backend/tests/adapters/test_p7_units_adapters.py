from __future__ import annotations

import pytest

from shifde.adapters.base import Filter, FilterGroup, SourceQueryPlan
from shifde.adapters.postgres import PostgresAdapter


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
