"""P6 pure units: schema compatibility, binding syntax, cron next-run (SETH `test_p6_units.py`).

The transform-operator, row-validation and untrusted-envelope tests of that file import
`shifde.runtime.pipeline_ops` (runtime): they move with it (docs/build/carry_forward.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from shifde.build.pipeline_nodes import (
    BINDING,
    PARAM_BINDING,
    ROWS,
    compatible,
    expand_refs,
)
from shifde.core.cron import Cron, CronError


# ------------------------------------------------------------------ schema compatibility
@pytest.mark.parametrize(
    ("src", "dst", "ok"),
    [
        ({}, {"type": "object"}, True),  # any is accepted everywhere
        ({"type": "object"}, {}, True),
        ({"type": "integer"}, {"type": "number"}, True),  # an integer is a number
        ({"type": "number"}, {"type": "integer"}, False),
        ({"type": "integer"}, {"type": ["array", "object"]}, False),
        ({"type": "array"}, {"type": ["array", "object"]}, True),
        ({"type": "string"}, {"type": "object"}, False),
        (ROWS, {"type": "array", "items": {"type": "object"}}, True),
        (
            {"type": "array", "items": {"type": "string"}},
            {"type": "array", "items": {"type": "object"}},
            False,
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "required": ["b"]},
            False,
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
            True,
        ),
        (
            {"type": "object", "properties": {"a": {"type": "integer"}}},
            {"type": "object", "properties": {"a": {"type": "string"}}},
            False,
        ),
    ],
)
def test_schema_compatibility(src: dict[str, Any], dst: dict[str, Any], ok: bool) -> None:
    assert (compatible(src, dst) is None) is ok


def test_named_schema_references_are_expanded() -> None:
    named = {
        "Row": {"type": "object", "properties": {"x": {"$ref": "#/schemas/Num"}}},
        "Num": {"type": "number"},
    }
    out = expand_refs({"type": "array", "items": {"$ref": "#/schemas/Row"}}, named)
    assert out == {
        "type": "array",
        "items": {"type": "object", "properties": {"x": {"type": "number"}}},
    }
    assert expand_refs({"$ref": "#/schemas/Missing"}, named) == {}


def test_binding_syntax() -> None:
    assert BINDING.match("src.rows") and BINDING.match("a_1.b_2")
    assert (
        not BINDING.match("src")
        and not BINDING.match("src.rows.x")
        and not BINDING.match("$params.x")
    )
    assert PARAM_BINDING.match("$params.region") and not PARAM_BINDING.match("params.region")


# ------------------------------------------------------------------ cron
def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expr", "after", "expected"),
    [
        ("*/15 * * * *", at(2026, 1, 1, 10, 7), at(2026, 1, 1, 10, 15)),
        ("*/15 * * * *", at(2026, 1, 1, 10, 45), at(2026, 1, 1, 11, 0)),
        ("0 9 * * *", at(2026, 1, 1, 9, 0), at(2026, 1, 2, 9, 0)),  # strictly after
        ("30 2 1 * *", at(2026, 1, 15, 0, 0), at(2026, 2, 1, 2, 30)),
        ("0 0 * * 1", at(2026, 1, 1, 0, 0), at(2026, 1, 5, 0, 0)),  # next Monday
        ("0 0 29 2 *", at(2026, 3, 1, 0, 0), at(2028, 2, 29, 0, 0)),  # leap day
        ("5,35 8-9 * * *", at(2026, 1, 1, 8, 36), at(2026, 1, 1, 9, 5)),
        ("0 0 1 * 0", at(2026, 1, 2, 0, 0), at(2026, 1, 4, 0, 0)),  # day-of-month OR weekday
        ("0 0 * * 7", at(2026, 1, 1, 0, 0), at(2026, 1, 4, 0, 0)),  # 7 = Sunday
    ],
)
def test_cron_next_run(expr: str, after: datetime, expected: datetime) -> None:
    assert Cron.parse(expr).next_after(after) == expected


def test_cron_time_zones_and_dst() -> None:
    cron = Cron.parse("0 9 * * *")
    assert cron.next_after(at(2026, 1, 1, 0, 0), "Europe/Berlin") == at(2026, 1, 1, 8, 0)
    assert cron.next_after(at(2026, 7, 1, 0, 0), "Europe/Berlin") == at(2026, 7, 1, 7, 0)
    # 02:30 does not exist on 2026-03-29 in Berlin (spring forward): that day is skipped
    gap = Cron.parse("30 2 * * *")
    assert gap.next_after(at(2026, 3, 28, 12, 0), "Europe/Berlin") == at(2026, 3, 30, 0, 30)


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "a * * * *",
        "*/0 * * * *",
        "5-1 * * * *",
    ],
)
def test_invalid_cron_is_rejected(expr: str) -> None:
    with pytest.raises(CronError):
        Cron.parse(expr)


def test_cron_rejects_unknown_zone_and_naive_time_and_impossible_dates() -> None:
    cron = Cron.parse("0 0 * * *")
    with pytest.raises(CronError):
        cron.next_after(at(2026, 1, 1), "Mars/Base")
    with pytest.raises(CronError):
        cron.next_after(datetime(2026, 1, 1))
    with pytest.raises(CronError):
        Cron.parse("0 0 31 2 *").next_after(at(2026, 1, 1))
