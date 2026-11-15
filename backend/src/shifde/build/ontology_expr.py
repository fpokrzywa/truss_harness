"""Ontology filter expressions: the safe expression language compiled to parameterized source
filters (P7 contract sections 2-4, amendment F1).

Three modes share one compiler:

* explorer / mapping filters name properties bare (`region == "north"`); a leading `row.` is
  accepted. Pipeline filters may also read `params.<name>` (constants of the run).
* policy row filters read `row.<property>` and `principal.<path>` (`principal.attributes.region`);
  a bare name is refused.

A comparison always has one *column* side (a mapped property) and one *value* side (a literal, a
`params.` constant or a principal value). Values are never spliced into SQL: the result is a tree of
`Filter` / `FilterGroup` objects whose values travel as bound parameters, and whose identifiers are
the mapped columns (the adapter quotes them). Everything the language cannot express safely
(column-to-column comparisons, calls, string containment, comparisons of JSON values) is refused
with a readable `FilterError`.

Fail closed: a principal value that is missing (no such attribute) raises `PrincipalValueMissing`,
which makes a policy not apply instead of comparing against NULL.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any  # Any: literal values of expressions / principal attributes

from shifde.adapters.base import Filter, FilterGroup, FilterNode
from shifde.core import safeexpr

# ---------------------------------------------------------------------------- type model
PROPERTY_TYPES = ("string", "integer", "number", "boolean", "date", "datetime", "json")
_COMPAT: dict[str, frozenset[str]] = {
    "string": frozenset(
        {"text", "character varying", "character", "varchar", "char", "bpchar", "uuid", "citext"}
    ),
    "integer": frozenset({"integer", "bigint", "smallint", "int", "int2", "int4", "int8"}),
    "number": frozenset(
        {
            "integer",
            "bigint",
            "smallint",
            "int",
            "int2",
            "int4",
            "int8",
            "numeric",
            "decimal",
            "real",
            "double precision",
            "float",
            "float4",
            "float8",
        }
    ),  # fmt: skip
    "boolean": frozenset({"boolean", "bool"}),
    "date": frozenset({"date"}),
    "datetime": frozenset(
        {
            "timestamp without time zone",
            "timestamp with time zone",
            "timestamp",
            "timestamptz",
            "date",
        }
    ),  # fmt: skip
    "json": frozenset({"json", "jsonb"}),
}
_PTYPE_OF_DB: dict[str, str] = {}
for _ptype in ("string", "integer", "number", "boolean", "date", "datetime", "json"):
    for _db in _COMPAT[_ptype]:
        _PTYPE_OF_DB.setdefault(_db, _ptype)
_PARAMS = re.compile(r"\(.*\)")


def normalize_db_type(db_type: str) -> str:
    return _PARAMS.sub("", db_type).strip().lower()


def type_compatible(ptype: str, db_type: str) -> bool:
    """May a source column of `db_type` back a property of `ptype`?"""
    return normalize_db_type(db_type) in _COMPAT.get(ptype, frozenset())


def ptype_of_db_type(db_type: str) -> str | None:
    """The property type a source column type reads as (None: unsupported type)."""
    return _PTYPE_OF_DB.get(normalize_db_type(db_type))


# ---------------------------------------------------------------------------- errors
class FilterError(ValueError):
    """The expression is not a valid ontology filter (message is safe to show)."""


class PrincipalValueMissing(FilterError):
    """A principal value an expression needs is not set: the expression cannot be evaluated."""


@dataclass(frozen=True)
class Operand:
    """The column side of a comparison: the source column, the property type, the property key."""

    column: str
    ptype: str
    key: str


Resolver = Callable[[tuple[str | int, ...]], Operand | None]


def coerce(ptype: str, value: Any) -> Any:  # Any: literal -> bound parameter value
    """The Python value to bind for a literal compared with a property of `ptype`."""
    if value is None:
        return None
    if ptype == "string":
        if isinstance(value, str):
            return value
    elif ptype == "integer":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif ptype == "number":
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    elif ptype == "boolean":
        if isinstance(value, bool):
            return value
    elif ptype == "date":
        if isinstance(value, str):
            try:
                return dt.date.fromisoformat(value)
            except ValueError as exc:
                raise FilterError(f"{value!r} is not an ISO date (YYYY-MM-DD)") from exc
    elif ptype == "datetime":
        if isinstance(value, str):
            try:
                parsed = dt.datetime.fromisoformat(value)
            except ValueError as exc:
                raise FilterError(f"{value!r} is not an ISO 8601 date-time") from exc
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    elif ptype == "json":
        raise FilterError("JSON properties can only be compared with null")
    raise FilterError(f"a {ptype} property cannot be compared with {type(value).__name__} value")


_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}
_OPS = {"==": "=", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def _lookup(context: Mapping[str, Any], path: tuple[str | int, ...]) -> Any:  # Any: JSON
    cur: Any = context
    for seg in path:
        if isinstance(cur, dict) and isinstance(seg, str):
            cur = cur.get(seg)
        else:
            return None
        if cur is None:
            return None
    return cur


@dataclass(frozen=True)
class _Value:
    value: Any  # Any: literal / constant / principal value
    source: str  # literal | const | principal


class _Compiler:
    def __init__(
        self,
        resolve: Resolver,
        principal: Mapping[str, Any] | None,
        consts: Mapping[str, Any] | None,
    ) -> None:
        self.resolve = resolve
        self.principal = principal
        self.consts = consts

    # ---------------------------------------------------------------- operands
    def operand(self, node: safeexpr.Node) -> Operand | _Value:
        if node[0] == "lit":
            return _Value(node[1], "literal")
        path: tuple[str | int, ...] = node[1]
        root = path[0]
        if root == "principal" and self.principal is not None:
            found = _lookup({"principal": self.principal}, path)
            if found is None:
                raise PrincipalValueMissing(
                    f"the principal value {'.'.join(map(str, path))} is not set"
                )
            if isinstance(found, dict):
                raise FilterError("a principal value must be a scalar or a list of scalars")
            return _Value(found, "principal")
        if root == "params" and self.consts is not None:
            found = _lookup({"params": self.consts}, path)
            if found is None:
                raise FilterError(f"the parameter {'.'.join(map(str, path[1:]))} is not set")
            if isinstance(found, dict | list):
                raise FilterError("a parameter used in a filter must be a scalar")
            return _Value(found, "const")
        operand = self.resolve(path)
        if operand is None:
            raise FilterError(f"unknown field {'.'.join(map(str, path))!r}")
        return operand

    # ---------------------------------------------------------------- tree
    def compile(self, node: safeexpr.Node) -> FilterNode:
        kind = node[0]
        if kind == "and" or kind == "or":
            return FilterGroup(kind, [self.compile(node[1]), self.compile(node[2])])
        if kind == "not":
            return FilterGroup("not", [self.compile(node[1])])
        if kind == "cmp":
            return self._cmp(node[1], node[2], node[3])
        if kind == "path":  # a bare boolean property
            operand = self.operand(node)
            if isinstance(operand, Operand) and operand.ptype == "boolean":
                return Filter(operand.column, "=", True)
            raise FilterError("only a boolean property can stand alone; compare it with a value")
        raise FilterError("a filter must compare a property with a value")

    def _cmp(self, op: str, left: safeexpr.Node, right: safeexpr.Node) -> FilterNode:
        a, b = self.operand(left), self.operand(right)
        if isinstance(a, Operand) and isinstance(b, Operand):
            raise FilterError("comparing two properties with each other is not supported")
        if isinstance(a, _Value) and isinstance(b, _Value):
            raise FilterError("a comparison needs a property on one side")
        if op == "in":
            if (
                not isinstance(a, Operand)
                or not isinstance(b, _Value)
                or not isinstance(b.value, list)
            ):
                raise FilterError("`in` is `<property> in <list from a principal attribute>`")
            if not b.value:
                return Filter(a.column, "in", [])
            values = [coerce(a.ptype, v) for v in b.value if not isinstance(v, dict | list)]
            return Filter(a.column, "in", values)
        if isinstance(a, _Value):  # value on the left: flip so the column is on the left
            a, b, op = b, a, _FLIP[op]
        assert isinstance(a, Operand) and isinstance(b, _Value)
        if isinstance(b.value, list):
            raise FilterError("a list can only be used with `in`")
        if b.value is None:
            if b.source != "literal":
                raise FilterError("a null value cannot be compared")
            if op == "==":
                return Filter(a.column, "is_null")
            if op == "!=":
                return FilterGroup("not", [Filter(a.column, "is_null")])
            raise FilterError("null can only be compared with == and !=")
        if a.ptype == "json":
            raise FilterError("JSON properties can only be compared with null")
        if op in ("<", "<=", ">", ">=") and a.ptype == "boolean":
            raise FilterError("a boolean property cannot be ordered")
        return Filter(a.column, _OPS[op], coerce(a.ptype, b.value))  # type: ignore[arg-type]


def compile_filter(
    source: str,
    resolve: Resolver,
    *,
    principal: Mapping[str, Any] | None = None,
    consts: Mapping[str, Any] | None = None,
) -> FilterNode:
    """Compile `source` (safe expression language) to a source filter tree.

    `resolve` maps a field path (`("region",)` / `("row", "region")`) to its column, or None when
    the path is not a field of the mapping. Raises `FilterError` (or `PrincipalValueMissing`)."""
    try:
        tree = safeexpr.parse(source)
    except safeexpr.ExprError as exc:
        raise FilterError(str(exc)) from exc
    return _Compiler(resolve, principal, consts).compile(tree)


def referenced_fields(source: str) -> set[tuple[str | int, ...]]:
    """Every field path of `source` (syntax check included)."""
    try:
        return safeexpr.paths(safeexpr.parse(source))
    except safeexpr.ExprError as exc:
        raise FilterError(str(exc)) from exc


def bare_resolver(columns: Mapping[str, Operand], seen: set[str] | None = None) -> Resolver:
    """Explorer / mapping filters: `prop` or `row.prop`. Records the property keys read."""

    def resolve(path: tuple[str | int, ...]) -> Operand | None:
        if len(path) == 2 and path[0] == "row":
            path = (path[1],)
        if len(path) != 1 or not isinstance(path[0], str):
            return None
        found = columns.get(path[0])
        if found is not None and seen is not None:
            seen.add(found.key)
        return found

    return resolve


def row_resolver(columns: Mapping[str, Operand]) -> Resolver:
    """Policy row filters: `row.prop` only."""

    def resolve(path: tuple[str | int, ...]) -> Operand | None:
        if len(path) == 2 and path[0] == "row" and isinstance(path[1], str):
            return columns.get(path[1])
        return None

    return resolve
