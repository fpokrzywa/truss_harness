"""Validation issues: `{path, code, message}` (contract section 1, `draft/validate`)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any  # Any: arbitrary JSON values being validated

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

MAX_SCHEMA_BYTES = 256_000
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    message: str
    node_id: str | None = None  # pipelines (P6 amendment E1): the node / edge the issue is about
    edge: tuple[str, str] | None = None
    key: str | None = None  # ontologies (P7 amendment F1): the relationship the issue is about

    def as_dict(self) -> dict[str, Any]:  # Any: optional node / edge members
        out: dict[str, Any] = {"path": self.path, "code": self.code, "message": self.message}
        if self.node_id is not None:
            out["node_id"] = self.node_id
        if self.key is not None:
            out["key"] = self.key
        if self.edge is not None:
            out["edge"] = {"from": self.edge[0], "to": self.edge[1]}
        return out


@dataclass
class Report:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def error(
        self,
        path: str,
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        edge: tuple[str, str] | None = None,
        key: str | None = None,
    ) -> None:
        self.errors.append(Issue(path, code, message, node_id, edge, key))

    def warn(
        self,
        path: str,
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        edge: tuple[str, str] | None = None,
        key: str | None = None,
    ) -> None:
        self.warnings.append(Issue(path, code, message, node_id, edge, key))

    @property
    def valid(self) -> bool:
        return not self.errors


def join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def _remote_refs(node: Any, found: list[str]) -> None:  # Any: JSON document
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key in ("$ref", "$dynamicRef")
                and isinstance(value, str)
                and not value.startswith("#")
            ):
                found.append(value)
            else:
                _remote_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _remote_refs(item, found)


def check_json_schema(report: Report, path: str, schema: Any) -> bool:  # Any: JSON document
    """Validate that `schema` is a JSON Schema draft 2020-12 document without remote references.
    Remote `$ref`s are refused outright: resolving them would be an SSRF vector."""
    if not isinstance(schema, dict):
        report.error(path, "invalid_schema", "must be a JSON Schema object")
        return False
    if len(repr(schema)) > MAX_SCHEMA_BYTES:
        report.error(path, "too_large", "schema is too large")
        return False
    refs: list[str] = []
    _remote_refs(schema, refs)
    if refs:
        report.error(path, "remote_ref_forbidden", "only local '#...' references are allowed")
        return False
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        report.error(path, "invalid_schema", f"not a valid JSON Schema (2020-12): {exc.message}")
        return False
    return True


def schema_errors(schema: dict[str, Any], instance: Any) -> list[tuple[list[str | int], str]]:
    """(path, message) of every violation of `instance` against a (previously checked) schema.
    The empty registry makes any unresolvable reference fail instead of being fetched."""
    validator = Draft202012Validator(schema, registry=Registry())
    out: list[tuple[list[str | int], str]] = []
    for error in sorted(validator.iter_errors(instance), key=lambda e: list(map(str, e.path))):
        out.append(([*error.absolute_path], error.message))
    return out


def schema_properties(schema: Any) -> set[str]:  # Any: JSON document
    props = schema.get("properties") if isinstance(schema, dict) else None
    return set(props) if isinstance(props, dict) else set()
