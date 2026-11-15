"""Pure data operations of the pipeline nodes (no I/O): the restricted transform operators, row /
document validation and the untrusted-data envelope. Kept separate from the executor so every
operator is unit-testable without a database.

Transform operators (amendment E1), applied in order to a list of rows (a single object counts as a
one-row list; the result is always a list of rows):

* `{op: "filter", expression}`            keep rows for which the safe expression is truthy
* `{op: "select", fields: [...]}`          keep only those fields
* `{op: "rename", from, to}`               rename one field
* `{op: "map", field, expression}`         set `field` to the expression's value
* `{op: "cast", field, to}`                string | number | integer | boolean
* `{op: "aggregate", group_by, fn, field?, as}`   count | sum | avg | min | max per group

Expressions use the orchestration safe-expression language over `row` and `params`; there is no
code execution and no attribute access beyond dictionary keys / list indexes.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any  # Any: JSON rows / values

from jsonschema import Draft202012Validator
from referencing import Registry

from shifde.core import safeexpr

MAX_ROWS = 100_000


class OpError(ValueError):
    """A transform / validation problem with the DATA (not the definition): fails the node."""


def _num(value: Any) -> float | None:  # Any: JSON value
    if isinstance(value, bool):
        return None
    try:
        out = float(value) if isinstance(value, int | float | str) else None
    except (ValueError, OverflowError):  # unparsable text, an integer beyond float range
        return None
    return out if out is not None and math.isfinite(out) else None


def sanitize_json(value: Any) -> Any:  # Any: JSON from adapters / models
    """Make external data storable: NUL removed, lone surrogates replaced, NaN / Infinity -> null
    (PostgreSQL jsonb cannot hold them, and they are not JSON)."""
    if isinstance(value, str):
        return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {sanitize_json(str(k)): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [sanitize_json(v) for v in value]
    return value


def _cast(value: Any, to: str, field: str) -> Any:  # Any: JSON value
    if value is None:
        return None
    if to == "string":
        return value if isinstance(value, str) else json.dumps(value, default=str)
    if to == "boolean":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes"):
                return True
            if lowered in ("false", "0", "no", ""):
                return False
            raise OpError(f"cast {field!r}: {value!r} is not a boolean")
        return bool(value)
    number = _num(value)
    if number is None:
        raise OpError(f"cast {field!r}: {value!r} is not a number")
    if to == "number":
        return int(number) if number.is_integer() and isinstance(value, int) else number
    if not number.is_integer():
        raise OpError(f"cast {field!r}: {value!r} is not an integer")
    return int(number)


def _eval(expression: str, row: Mapping[str, Any], params: Mapping[str, Any]) -> Any:  # Any: JSON
    try:
        return safeexpr.evaluate(safeexpr.parse(expression), {"row": dict(row), "params": params})
    except safeexpr.ExprError as exc:
        raise OpError(str(exc)) from exc


def _aggregate(rows: list[dict[str, Any]], op: Mapping[str, Any]) -> list[dict[str, Any]]:
    group_by: list[str] = list(op.get("group_by") or [])
    fn = str(op["fn"])
    fld = op.get("field")
    out_name = str(op["as"])
    groups: dict[tuple[str, ...], tuple[list[Any], list[dict[str, Any]]]] = {}
    for row in rows:
        values = [row.get(g) for g in group_by]
        key = tuple(json.dumps(v, sort_keys=True, default=str) for v in values)
        groups.setdefault(key, (values, []))[1].append(row)
    if not group_by and not groups:
        groups[()] = ([], [])  # the empty aggregate still has one (neutral) result row
    result: list[dict[str, Any]] = []
    for values, members in groups.values():
        entry: dict[str, Any] = dict(zip(group_by, values, strict=True))
        if fn == "count":
            entry[out_name] = (
                len(members)
                if fld is None
                else sum(1 for m in members if m.get(str(fld)) is not None)
            )
        else:
            numbers: list[float] = []
            for m in members:
                raw = m.get(str(fld))
                if raw is None:
                    continue
                number = _num(raw)
                if number is None:
                    raise OpError(f"aggregate {fn} of {fld!r}: {raw!r} is not a number")
                numbers.append(number)
            if fn == "sum":
                total = sum(numbers)
                if not math.isfinite(total):
                    raise OpError(f"aggregate sum of {fld!r} overflows")
                entry[out_name] = int(total) if float(total).is_integer() else total
            elif not numbers:
                entry[out_name] = None
            elif fn == "avg":
                mean = sum(numbers) / len(numbers)
                if not math.isfinite(mean):
                    raise OpError(f"aggregate avg of {fld!r} overflows")
                entry[out_name] = mean
            else:
                best = min(numbers) if fn == "min" else max(numbers)
                entry[out_name] = int(best) if best.is_integer() else best
        result.append(entry)
    return result


def apply_transform(
    data: Any,  # Any: a list of rows or one object
    operations: Sequence[Mapping[str, Any]],
    params: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        rows: list[dict[str, Any]] = [dict(data)]
    elif isinstance(data, list) and all(isinstance(r, dict) for r in data):
        rows = [dict(r) for r in data]
    else:
        raise OpError("transform input must be a list of objects or an object")
    if len(rows) > MAX_ROWS:
        raise OpError(f"transform input has more than {MAX_ROWS} rows")
    for op in operations:
        kind = op.get("op")
        if kind == "filter":
            rows = [r for r in rows if safeexpr.truthy(_eval(str(op["expression"]), r, params))]
        elif kind == "select":
            fields = [str(f) for f in op["fields"]]
            rows = [{f: r.get(f) for f in fields} for r in rows]
        elif kind == "rename":
            old, new = str(op["from"]), str(op["to"])
            rows = [
                {(new if k == old else k): v for k, v in r.items()} if old in r else dict(r)
                for r in rows
            ]
        elif kind == "map":
            field = str(op["field"])
            rows = [{**r, field: _eval(str(op["expression"]), r, params)} for r in rows]
        elif kind == "cast":
            field, to = str(op["field"]), str(op["to"])
            rows = [{**r, field: _cast(r.get(field), to, field)} if field in r else r for r in rows]
        elif kind == "aggregate":
            rows = _aggregate(rows, op)
        else:
            raise OpError(f"unsupported operator {kind!r}")
    return rows


def validate_rows(
    schema: dict[str, Any],
    data: Any,  # Any: rows or a document
) -> tuple[list[Any], list[dict[str, Any]], bool]:
    """(valid items, invalid `{row, errors}` entries, input_was_a_list). A list is validated row by
    row (the schema describes one row); anything else is validated as one document."""
    validator = Draft202012Validator(schema, registry=Registry())

    def problems(instance: Any) -> list[str]:  # Any: JSON value
        found = sorted(validator.iter_errors(instance), key=lambda e: list(map(str, e.path)))
        return [
            f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"[:300]
            for e in found[:5]
        ]

    if isinstance(data, list):
        valid: list[Any] = []
        invalid: list[dict[str, Any]] = []
        for item in data:
            errors = problems(item)
            if errors:
                invalid.append({"row": item, "errors": errors})
            else:
                valid.append(item)
        return valid, invalid, True
    errors = problems(data)
    return ([] if errors else [data]), ([{"row": data, "errors": errors}] if errors else []), False


def untrusted_envelope(source: str, value: Any) -> str:  # Any: pipeline data
    """Pipeline data handed to a model: delimited, escaped, and labelled as data. It only ever
    appears in a user-role message, never in the system message."""
    payload = value if isinstance(value, str) else json.dumps(value, default=str)
    return (
        f'<untrusted source="{html.escape(source)}">{html.escape(payload, quote=False)}</untrusted>'
    )
