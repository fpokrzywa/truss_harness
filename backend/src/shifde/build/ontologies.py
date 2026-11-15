"""Ontology specs and source mappings: validation before publish (docs/api/PHASE_7_CONTRACT.md
sections 1-2 and amendment F1).

The spec is the LOGICAL model only (namespaces, object types, properties, identifiers,
relationships, actions, presentation); the physical layer is the separate set of source mappings
of the draft. Publish needs both to be consistent: every object type and relationship has a
mapping, every mapped relation / column exists in the connection's latest schema discovery with a
compatible type, and every connection is active and declares `query.sql`. Issue codes are the
contract's (`duplicate_key, unknown_namespace, invalid_type, invalid_primary_key,
dangling_relationship, unmapped_object_type, unmapped_relationship, mapping_invalid,
type_incompatible, capability_missing, action_tool_invalid`) plus `invalid_value` /
`invalid_cardinality` for malformed values. Object-type issues carry `node_id`, relationship
issues `key` (amendment F1).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any  # Any: stored JSON specs
from uuid import UUID

from shifde.build import ontology_expr as expr
from shifde.build.canonical import spec_hash
from shifde.build.env import ConnectionInfo, Env, VersionRef
from shifde.build.issues import Report

KNOWN_KEYS = frozenset({"namespaces", "object_types", "relationships", "actions", "ui"})
PROPERTY_TYPES = expr.PROPERTY_TYPES
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
CARDINALITIES = ("one_to_one", "one_to_many", "many_to_one", "many_to_many")
KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
PROP_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
RESERVED = frozenset({"and", "or", "not", "in", "true", "false", "null", "row", "principal"})
MAX_KEY = 100
MAX_OBJECT_TYPES = 200
MAX_PROPERTIES = 200
MAX_RELATIONSHIPS = 500
MAX_ACTIONS = 100
MAX_FILTERS = 20
MAX_COLUMN = 128
OBJECT_REF = "$object."
WRITE_CLASSES = frozenset({"write_idempotent", "write_non_idempotent", "destructive"})


@dataclass
class OntologyResult:
    spec: dict[str, Any]
    pins: list[tuple[VersionRef, str]] = field(default_factory=list)
    connection_ids: set[UUID] = field(default_factory=set)
    mappings_hash: str = ""


@dataclass
class MappingCheck:
    ok: bool
    issues: list[dict[str, Any]]
    sample_columns: list[dict[str, Any]]


# ============================================================================ helpers
def mappings_digest(mappings: Sequence[Mapping[str, Any]]) -> str:
    """Canonical hash of the mappings of a version (frozen at publish; re-checked per query)."""
    docs = sorted(
        (
            {
                "target_kind": m["target_kind"],
                "target_key": m["target_key"],
                "connection_id": str(m["connection_id"]),
                "relation": [m["relation_schema"], m["relation_name"]],
                "column_map": dict(m["column_map"] or {}),
                "key_map": m["key_map"],
                "filters": list(m["filters"] or []),
            }
            for m in mappings
        ),
        key=lambda d: (d["target_kind"], d["target_key"]),
    )
    return spec_hash({"mappings": docs})


def _uuid(value: Any) -> UUID | None:  # Any: JSON value
    try:
        return UUID(str(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _is_int(value: Any) -> bool:  # Any: JSON value
    return isinstance(value, int) and not isinstance(value, bool)


def mapping_path(kind: str, key: str) -> str:
    return f"mappings/{kind}/{key}"


class _Tables:
    """The tables of one discovery payload, by (schema, name)."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.by_name: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for table in payload.get("tables") or []:
            if not isinstance(table, dict):
                continue
            cols = {
                str(c["name"]): c
                for c in table.get("columns") or []
                if isinstance(c, dict) and "name" in c
            }
            self.by_name[(str(table.get("schema", "")), str(table.get("name", "")))] = cols

    def find(self, schema: str, name: str) -> tuple[str, dict[str, dict[str, Any]]] | str:
        """(resolved schema, columns) or an error message."""
        if schema:
            cols = self.by_name.get((schema, name))
            return (schema, cols) if cols is not None else f"relation {schema}.{name} not found"
        matches = [(s, c) for (s, n), c in self.by_name.items() if n == name]
        if not matches:
            return f"relation {name} not found in the connection's schema discovery"
        if len(matches) > 1:
            public = [m for m in matches if m[0] == "public"]
            if len(public) == 1:
                return public[0]
            return f"relation {name} is ambiguous (several schemas); name the schema"
        return matches[0]


# ============================================================================ logical model
@dataclass
class _Model:
    namespaces: dict[str, dict[str, Any]]
    types: dict[str, dict[str, Any]]
    relationships: dict[str, dict[str, Any]]
    type_index: dict[str, int]
    rel_index: dict[str, int]


def _label(item: dict[str, Any], key: str) -> str:  # Any: JSON
    label = item.get("label")
    return label if isinstance(label, str) and label.strip() else key


def _validate_namespaces(raw: Any, report: Report) -> dict[str, dict[str, Any]]:  # Any: JSON
    out: dict[str, dict[str, Any]] = {}
    if raw is None:
        return out
    if not isinstance(raw, list):
        report.error("namespaces", "invalid_type", "must be a list")
        return out
    for i, ns in enumerate(raw):
        path = f"namespaces[{i}]"
        if not isinstance(ns, dict):
            report.error(path, "invalid_type", "must be an object")
            continue
        key = ns.get("key")
        if not isinstance(key, str) or not KEY.match(key) or len(key) > MAX_KEY:
            report.error(f"{path}.key", "invalid_value", "must be a lowercase key (a-z, 0-9, _)")
            continue
        if key in out:
            report.error(f"{path}.key", "duplicate_key", f"namespace {key!r} is used twice")
            continue
        out[key] = {"key": key, "label": _label(ns, key)}
    return out


def _validate_property(
    prop: Any,
    path: str,
    ot_key: str,
    seen: set[str],
    report: Report,  # Any: JSON
) -> dict[str, Any] | None:
    if not isinstance(prop, dict):
        report.error(path, "invalid_type", "must be an object", node_id=ot_key)
        return None
    key = prop.get("key")
    if not isinstance(key, str) or not PROP_KEY.match(key) or key in RESERVED:
        report.error(
            f"{path}.key",
            "invalid_value",
            "a property key is a letter followed by letters, digits or _ (not a reserved word)",
            node_id=ot_key,
        )
        return None
    if key in seen:
        report.error(
            f"{path}.key", "duplicate_key", f"property {key!r} is used twice", node_id=ot_key
        )
        return None
    seen.add(key)
    ptype = prop.get("type")
    if ptype not in PROPERTY_TYPES:
        report.error(
            f"{path}.type",
            "invalid_type",
            f"type must be one of {', '.join(PROPERTY_TYPES)}",
            node_id=ot_key,
        )
    nullable = prop.get("nullable", True)
    if not isinstance(nullable, bool):
        report.error(f"{path}.nullable", "invalid_type", "must be true or false", node_id=ot_key)
        nullable = True
    classification = prop.get("classification", "internal")
    if classification not in CLASSIFICATIONS:
        report.error(
            f"{path}.classification",
            "invalid_value",
            f"classification must be one of {', '.join(CLASSIFICATIONS)}",
            node_id=ot_key,
        )
        classification = "internal"
    return {
        "key": key,
        "type": ptype if ptype in PROPERTY_TYPES else "string",
        "nullable": nullable,
        "classification": classification,
        "label": _label(prop, key),
    }


def _validate_object_types(
    raw: Any,
    namespaces: dict[str, dict[str, Any]],
    report: Report,  # Any: JSON
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    types: dict[str, dict[str, Any]] = {}
    index: dict[str, int] = {}
    if raw is None:
        return types, index
    if not isinstance(raw, list):
        report.error("object_types", "invalid_type", "must be a list")
        return types, index
    if len(raw) > MAX_OBJECT_TYPES:
        report.error("object_types", "too_many", f"at most {MAX_OBJECT_TYPES} object types")
        raw = raw[:MAX_OBJECT_TYPES]
    for i, ot in enumerate(raw):
        path = f"object_types[{i}]"
        if not isinstance(ot, dict):
            report.error(path, "invalid_type", "must be an object")
            continue
        key = ot.get("key")
        if not isinstance(key, str) or not KEY.match(key) or len(key) > MAX_KEY:
            report.error(
                f"{path}.key", "invalid_value", "must be a lowercase key such as crm.customer"
            )
            continue
        if key in types:
            report.error(
                f"{path}.key", "duplicate_key", f"object type {key!r} is used twice", node_id=key
            )
            continue
        ns = ot.get("namespace")
        if ns not in namespaces:
            report.error(
                f"{path}.namespace",
                "unknown_namespace",
                f"namespace {ns!r} is not declared",
                node_id=key,
            )
        props_raw = ot.get("properties")
        props: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not isinstance(props_raw, list) or not props_raw:
            report.error(f"{path}.properties", "invalid_type", "at least one property", node_id=key)
        else:
            if len(props_raw) > MAX_PROPERTIES:
                report.error(
                    f"{path}.properties", "too_many", f"at most {MAX_PROPERTIES}", node_id=key
                )
                props_raw = props_raw[:MAX_PROPERTIES]
            for j, prop in enumerate(props_raw):
                stored = _validate_property(prop, f"{path}.properties[{j}]", key, seen, report)
                if stored is not None:
                    props.append(stored)
        pk = ot.get("primary_key")
        by_key = {p["key"]: p for p in props}
        if (
            not isinstance(pk, list)
            or not pk
            or not all(isinstance(k, str) for k in pk)
            or len(set(pk)) != len(pk)
        ):
            report.error(
                f"{path}.primary_key",
                "invalid_primary_key",
                "primary_key is a non-empty list of distinct property keys",
                node_id=key,
            )
            pk = []
        else:
            for k in pk:
                if k not in by_key:
                    report.error(
                        f"{path}.primary_key",
                        "invalid_primary_key",
                        f"primary key property {k!r} does not exist",
                        node_id=key,
                    )
                elif by_key[k]["nullable"]:
                    report.error(
                        f"{path}.primary_key",
                        "invalid_primary_key",
                        f"primary key property {k!r} must not be nullable",
                        node_id=key,
                    )
                elif by_key[k]["type"] == "json":
                    report.error(
                        f"{path}.primary_key",
                        "invalid_primary_key",
                        f"primary key property {k!r} cannot be JSON",
                        node_id=key,
                    )
        display = ot.get("display_property")
        if display is not None and (not isinstance(display, str) or display not in by_key):
            report.error(
                f"{path}.display_property",
                "invalid_value",
                "display_property must name a property of the object type",
                node_id=key,
            )
            display = None
        stored_ot: dict[str, Any] = {
            "key": key,
            "label": _label(ot, key),
            "namespace": ns if isinstance(ns, str) else "",
            "properties": props,
            "primary_key": list(pk),
        }
        if display is not None:
            stored_ot["display_property"] = display
        types[key] = stored_ot
        index[key] = i
    return types, index


def _validate_relationships(
    raw: Any,
    types: dict[str, dict[str, Any]],
    report: Report,  # Any: JSON
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    rels: dict[str, dict[str, Any]] = {}
    index: dict[str, int] = {}
    if raw is None:
        return rels, index
    if not isinstance(raw, list):
        report.error("relationships", "invalid_type", "must be a list")
        return rels, index
    if len(raw) > MAX_RELATIONSHIPS:
        report.error("relationships", "too_many", f"at most {MAX_RELATIONSHIPS} relationships")
        raw = raw[:MAX_RELATIONSHIPS]
    for i, rel in enumerate(raw):
        path = f"relationships[{i}]"
        if not isinstance(rel, dict):
            report.error(path, "invalid_type", "must be an object")
            continue
        key = rel.get("key")
        if not isinstance(key, str) or not KEY.match(key) or len(key) > MAX_KEY:
            report.error(f"{path}.key", "invalid_value", "must be a lowercase key")
            continue
        if key in rels:
            report.error(
                f"{path}.key", "duplicate_key", f"relationship {key!r} is used twice", key=key
            )
            continue
        for end in ("from", "to"):
            if rel.get(end) not in types:
                report.error(
                    f"{path}.{end}",
                    "dangling_relationship",
                    f"the {end} object type {rel.get(end)!r} does not exist",
                    key=key,
                )
        card = rel.get("cardinality")
        if card not in CARDINALITIES:
            report.error(
                f"{path}.cardinality",
                "invalid_cardinality",
                f"cardinality must be one of {', '.join(CARDINALITIES)}",
                key=key,
            )
        constraints = rel.get("constraints", {})
        if not isinstance(constraints, dict) or not isinstance(
            constraints.get("required", False), bool
        ):
            report.error(
                f"{path}.constraints", "invalid_type", "constraints is {required: bool}", key=key
            )
            constraints = {}
        rels[key] = {
            "key": key,
            "label": _label(rel, key),
            "from": rel.get("from"),
            "to": rel.get("to"),
            "cardinality": card if card in CARDINALITIES else "one_to_many",
            "constraints": {"required": bool(constraints.get("required", False))},
        }
        index[key] = i
    return rels, index


def _validate_ui(raw: Any, report: Report) -> dict[str, Any]:  # Any: JSON, presentation only
    if raw is None:
        return {}
    positions = raw.get("positions") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or (positions is not None and not isinstance(positions, dict)):
        report.error("ui", "invalid_type", "ui.positions must be an object")
        return {}
    for nid, pos in (positions or {}).items():
        ok = isinstance(pos, dict) and all(
            isinstance(pos.get(a), int | float) and not isinstance(pos.get(a), bool) for a in "xy"
        )
        if not ok:
            report.error(f"ui.positions.{nid}", "invalid_value", "expected {x: number, y: number}")
    return dict(raw)


# ============================================================================ actions
def _validate_actions(
    raw: Any,  # Any: JSON
    model: _Model,
    env: Env,
    report: Report,
    result: OntologyResult,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        report.error("actions", "invalid_type", "must be a list")
        return out
    if len(raw) > MAX_ACTIONS:
        report.error("actions", "too_many", f"at most {MAX_ACTIONS} actions")
        raw = raw[:MAX_ACTIONS]
    seen: set[str] = set()
    for i, action in enumerate(raw):
        path = f"actions[{i}]"
        if not isinstance(action, dict):
            report.error(path, "invalid_type", "must be an object")
            continue
        key = action.get("key")
        if not isinstance(key, str) or not KEY.match(key) or len(key) > MAX_KEY:
            report.error(f"{path}.key", "invalid_value", "must be a lowercase key")
            continue
        if key in seen:
            report.error(f"{path}.key", "duplicate_key", f"action {key!r} is used twice")
            continue
        seen.add(key)
        stored: dict[str, Any] = dict(action)
        ot_key = action.get("object_type")
        ot = model.types.get(ot_key) if isinstance(ot_key, str) else None
        if ot is None:
            report.error(
                f"{path}.object_type", "action_tool_invalid", f"unknown object type {ot_key!r}"
            )
        mapping = action.get("param_mapping", {})
        if not isinstance(mapping, dict) or not all(
            isinstance(k, str) and (v is None or isinstance(v, str | int | float | bool))
            for k, v in mapping.items()
        ):
            report.error(
                f"{path}.param_mapping",
                "action_tool_invalid",
                "param_mapping maps tool parameters to $object.<property> or a constant",
            )
            mapping = {}
        if ot is not None:
            props = {p["key"] for p in ot["properties"]}
            for param, value in mapping.items():
                is_ref = isinstance(value, str) and value.startswith(OBJECT_REF)
                if is_ref and str(value)[len(OBJECT_REF) :] not in props:
                    report.error(
                        f"{path}.param_mapping.{param}",
                        "action_tool_invalid",
                        f"{value!r} does not name a property of {ot_key}",
                    )
        stored["param_mapping"] = dict(mapping)
        vid = _uuid(action.get("tool_version_id"))
        found = env.version(vid) if vid is not None else None
        where = f"{path}.tool_version_id"
        if found is None:
            report.error(where, "action_tool_invalid", "no such tool version is available to you")
        elif found.kind != "tools":
            report.error(where, "action_tool_invalid", "expected a tool version")
        elif found.state == "draft":
            report.error(where, "action_tool_invalid", "only published tool versions can be used")
        elif found.state == "retired" or found.artifact_status in ("retired", "disabled"):
            report.error(where, "action_tool_invalid", "the tool is retired or disabled")
        elif not env.permits("tool:invoke", found):
            report.error(where, "action_tool_invalid", "you may not use this tool")
        else:
            schema = found.spec.get("input_schema")
            declared = schema.get("properties") if isinstance(schema, dict) else None
            if isinstance(declared, dict):
                for param in mapping:
                    if param not in declared:
                        report.error(
                            f"{path}.param_mapping.{param}",
                            "action_tool_invalid",
                            "the tool declares no such parameter",
                        )
                required = schema.get("required") if isinstance(schema, dict) else None
                missing = [r for r in required or [] if r not in mapping]
                if missing:
                    report.warn(
                        f"{path}.param_mapping",
                        "action_param_unbound",
                        f"required tool parameters {sorted(missing)} must come from extra_params",
                    )
            if found.spec.get("side_effect_class") in WRITE_CLASSES:
                report.warn(
                    path,
                    "action_requires_approval",
                    "this action has a side effect: every invocation waits for an approval",
                )
            result.pins.append((found, "action_tool"))
            stored["tool_version_id"] = str(found.version_id)
        out.append(stored)
    return out


# ============================================================================ mappings
def _mapping_error(
    report: Report, kind: str, key: str, code: str, message: str, ctx: str = ""
) -> None:
    suffix = f".{ctx}" if ctx else ""
    path = mapping_path(kind, key) + suffix
    if kind == "object_type":
        report.error(path, code, message, node_id=key)
    else:
        report.error(path, code, message, key=key)


def _connection_checks(
    info: ConnectionInfo | None, kind: str, key: str, report: Report, check_use: bool
) -> bool:
    if info is None:
        _mapping_error(
            report, kind, key, "mapping_invalid", "no such connection is available to you"
        )
        return False
    ok = True
    if check_use and not info.usable_by_publisher:
        _mapping_error(report, kind, key, "mapping_invalid", "you may not use this connection")
        ok = False
    if info.connection_type != "database":
        _mapping_error(report, kind, key, "mapping_invalid", "a database connection is required")
        ok = False
    if info.status != "active":
        _mapping_error(report, kind, key, "mapping_invalid", f"the connection is {info.status}")
        ok = False
    if not info.implemented or "query.sql" not in info.capabilities:
        _mapping_error(
            report,
            kind,
            key,
            "capability_missing",
            "the connection's adapter does not declare the query.sql capability",
        )
        ok = False
    elif kind == "relationship" and "query.pushdown_join" not in info.capabilities:
        report.warn(
            mapping_path(kind, key),
            "keyed_fetch",
            "the adapter does not push joins down: this relationship is traversed by keyed fetch",
            key=key,
        )
    return ok


def _check_filters(
    mapping: Mapping[str, Any],
    columns: Mapping[str, expr.Operand],
    kind: str,
    key: str,
    report: Report,
) -> None:
    filters = list(mapping.get("filters") or [])
    if len(filters) > MAX_FILTERS:
        _mapping_error(report, kind, key, "mapping_invalid", f"at most {MAX_FILTERS} filters")
        return
    for n, source in enumerate(filters):
        if not isinstance(source, str):
            _mapping_error(
                report, kind, key, "mapping_invalid", "a filter is an expression", f"filters[{n}]"
            )
            continue
        try:
            expr.compile_filter(source, expr.bare_resolver(columns))
        except expr.FilterError as exc:
            _mapping_error(
                report, kind, key, "mapping_invalid", f"filter {n + 1}: {exc}", f"filters[{n}]"
            )


def _validate_object_mapping(
    mapping: Mapping[str, Any],
    ot: dict[str, Any],
    tables: _Tables | None,
    report: Report,
) -> list[dict[str, Any]]:
    """Checks an object-type mapping against discovery; returns the relation's sample columns."""
    key = ot["key"]
    kind = "object_type"
    props = {p["key"]: p for p in ot["properties"]}
    column_map = dict(mapping.get("column_map") or {})
    for prop_key, column in column_map.items():
        if prop_key not in props:
            _mapping_error(
                report, kind, key, "mapping_invalid", f"{prop_key!r} is not a property of {key}"
            )
        elif not isinstance(column, str) or not column or len(column) > MAX_COLUMN:
            _mapping_error(
                report, kind, key, "mapping_invalid", f"column of {prop_key!r} is invalid"
            )
    for pk in ot["primary_key"]:
        if pk not in column_map:
            _mapping_error(
                report, kind, key, "mapping_invalid", f"primary key property {pk!r} is not mapped"
            )
    unmapped = [k for k in props if k not in column_map and k not in ot["primary_key"]]
    if unmapped:
        report.warn(
            mapping_path(kind, key),
            "unmapped_property",
            f"properties {sorted(unmapped)} are not mapped (they cannot be queried)",
            node_id=key,
        )
    if tables is None:
        return []
    found = tables.find(str(mapping["relation_schema"]), str(mapping["relation_name"]))
    if isinstance(found, str):
        _mapping_error(report, kind, key, "mapping_invalid", found, "relation")
        return []
    _, cols = found
    operands: dict[str, expr.Operand] = {}
    for prop_key, column in column_map.items():
        prop = props.get(prop_key)
        if prop is None or not isinstance(column, str):
            continue
        col = cols.get(column)
        if col is None:
            _mapping_error(
                report,
                kind,
                key,
                "mapping_invalid",
                f"column {column!r} does not exist",
                f"column_map.{prop_key}",
            )
            continue
        db_type = str(col.get("type", ""))
        if not expr.type_compatible(prop["type"], db_type):
            _mapping_error(
                report,
                kind,
                key,
                "type_incompatible",
                f"property {prop_key!r} ({prop['type']}) cannot be backed by "
                f"{column!r} ({db_type})",
                f"column_map.{prop_key}",
            )
            continue
        if not prop["nullable"] and col.get("nullable", True) and prop_key not in ot["primary_key"]:
            report.warn(
                mapping_path(kind, key) + f".column_map.{prop_key}",
                "nullability",
                f"property {prop_key!r} is not nullable but column {column!r} is",
                node_id=key,
            )
        operands[prop_key] = expr.Operand(column, prop["type"], prop_key)
    _check_filters(mapping, operands, kind, key, report)
    return [
        {"name": n, "type": c.get("type"), "nullable": c.get("nullable")} for n, c in cols.items()
    ]


def _validate_relationship_mapping(
    mapping: Mapping[str, Any],
    rel: dict[str, Any],
    types: dict[str, dict[str, Any]],
    tables: _Tables | None,
    report: Report,
) -> list[dict[str, Any]]:
    key = rel["key"]
    kind = "relationship"
    key_map = mapping.get("key_map")
    ends = [types.get(rel["from"]), types.get(rel["to"])]
    for end, ot in zip(("from", "to"), ends, strict=True):
        if ot is not None and len(ot["primary_key"]) != 1:
            _mapping_error(
                report,
                kind,
                key,
                "mapping_invalid",
                f"the {end} object type has a composite primary key; relationships need a "
                "single-column key",
            )
            return []
    if not isinstance(key_map, dict) or not all(
        isinstance(key_map.get(c), str) and key_map.get(c) for c in ("from_column", "to_column")
    ):
        _mapping_error(
            report,
            kind,
            key,
            "mapping_invalid",
            "key_map needs from_column and to_column",
            "key_map",
        )
        return []
    link = key_map.get("relation")
    if link is not None:
        same = (
            isinstance(link, dict)
            and str(link.get("schema", "")) == str(mapping["relation_schema"])
            and str(link.get("name", "")) == str(mapping["relation_name"])
        )
        if not same:
            _mapping_error(
                report,
                kind,
                key,
                "mapping_invalid",
                "key_map.relation must repeat the mapping's relation (the link table)",
                "key_map.relation",
            )
            return []
    if tables is None:
        return []
    found = tables.find(str(mapping["relation_schema"]), str(mapping["relation_name"]))
    if isinstance(found, str):
        _mapping_error(report, kind, key, "mapping_invalid", found, "relation")
        return []
    _, cols = found
    for end, column_key, ot in zip(("from", "to"), ("from_column", "to_column"), ends, strict=True):
        column = str(key_map[column_key])
        col = cols.get(column)
        if col is None:
            _mapping_error(
                report,
                kind,
                key,
                "mapping_invalid",
                f"column {column!r} does not exist",
                f"key_map.{column_key}",
            )
            continue
        if ot is not None:
            pk_prop = next(p for p in ot["properties"] if p["key"] == ot["primary_key"][0])
            if not expr.type_compatible(pk_prop["type"], str(col.get("type", ""))):
                _mapping_error(
                    report,
                    kind,
                    key,
                    "type_incompatible",
                    f"{column!r} ({col.get('type')}) cannot hold the {end} key "
                    f"{pk_prop['key']!r} ({pk_prop['type']})",
                    f"key_map.{column_key}",
                )
    operands: dict[str, expr.Operand] = {}
    for name, col in cols.items():
        ptype = expr.ptype_of_db_type(str(col.get("type", "")))
        if ptype is not None:
            operands[name] = expr.Operand(name, ptype, name)
    _check_filters(mapping, operands, kind, key, report)
    return [
        {"name": n, "type": c.get("type"), "nullable": c.get("nullable")} for n, c in cols.items()
    ]


def validate_mapping(
    mapping: Mapping[str, Any],
    model_types: dict[str, dict[str, Any]],
    model_rels: dict[str, dict[str, Any]],
    env: Env,
    report: Report,
    *,
    check_use: bool = True,
    discovery: dict[UUID, _Tables | None] | None = None,
) -> list[dict[str, Any]]:
    """Validate one stored mapping against the logical model and the connection; returns the
    relation's columns (for the mapping check endpoint)."""
    kind, key = str(mapping["target_kind"]), str(mapping["target_key"])
    ot = model_types.get(key) if kind == "object_type" else None
    rel = model_rels.get(key) if kind == "relationship" else None
    if ot is None and rel is None:
        _mapping_error(
            report,
            kind,
            key,
            "mapping_invalid",
            f"the ontology has no such {kind.replace('_', ' ')}",
        )
        return []
    cid = mapping["connection_id"]
    info = env.connection(cid)
    if not _connection_checks(info, kind, key, report, check_use):
        return []
    cache = discovery if discovery is not None else {}
    if cid not in cache:
        payload = env.schema_discovery(cid)
        cache[cid] = _Tables(payload) if payload is not None else None
    tables = cache[cid]
    if tables is None:
        _mapping_error(
            report,
            kind,
            key,
            "mapping_invalid",
            "the connection has no schema discovery: run discovery on the connection first",
        )
        return []
    if ot is not None:
        return _validate_object_mapping(mapping, ot, tables, report)
    assert rel is not None
    return _validate_relationship_mapping(mapping, rel, model_types, tables, report)


# ============================================================================ entry points
def validate_ontology(
    spec: dict[str, Any],
    mappings: Sequence[Mapping[str, Any]],
    env: Env,
    report: Report,
    *,
    resolve: bool = True,
    check_use: bool = True,
) -> OntologyResult:
    """Validate a draft spec together with its mappings. Returns the spec to store (defaults
    filled), the tool pins, the connections used and the mappings hash."""
    for key in spec:
        if key not in KNOWN_KEYS:
            report.warn(key, "unknown_field", "field is ignored")
    namespaces = _validate_namespaces(spec.get("namespaces"), report)
    types, type_index = _validate_object_types(spec.get("object_types"), namespaces, report)
    rels, rel_index = _validate_relationships(spec.get("relationships"), types, report)
    model = _Model(namespaces, types, rels, type_index, rel_index)
    out: dict[str, Any] = {
        "namespaces": list(namespaces.values()),
        "object_types": list(types.values()),
        "relationships": list(rels.values()),
    }
    result = OntologyResult(out)
    out["actions"] = _validate_actions(spec.get("actions"), model, env, report, result)
    out["ui"] = _validate_ui(spec.get("ui"), report)

    by_target = {(str(m["target_kind"]), str(m["target_key"])): m for m in mappings}
    for key in types:
        if ("object_type", key) not in by_target:
            report.error(
                f"object_types[{type_index[key]}]",
                "unmapped_object_type",
                f"object type {key!r} has no source mapping",
                node_id=key,
            )
    for key in rels:
        if ("relationship", key) not in by_target:
            report.error(
                f"relationships[{rel_index[key]}]",
                "unmapped_relationship",
                f"relationship {key!r} has no source mapping",
                key=key,
            )
    cache: dict[UUID, _Tables | None] = {}
    for mapping in mappings:
        validate_mapping(mapping, types, rels, env, report, check_use=check_use, discovery=cache)
        result.connection_ids.add(mapping["connection_id"])
    result.mappings_hash = mappings_digest(mappings)
    return result


def check_one_mapping(
    spec: dict[str, Any], mapping: Mapping[str, Any], env: Env, *, check_use: bool = True
) -> MappingCheck:
    """The mapping `check` endpoint: the mapping against the draft's logical model and the
    connection's schema discovery. Metadata only; no rows are read."""
    scratch = Report()
    namespaces = _validate_namespaces(spec.get("namespaces"), scratch)
    types, _ = _validate_object_types(spec.get("object_types"), namespaces, scratch)
    rels, _ = _validate_relationships(spec.get("relationships"), types, scratch)
    report = Report()
    columns = validate_mapping(mapping, types, rels, env, report, check_use=check_use)
    issues = [
        {"code": i.code, "message": i.message, "path": i.path, "severity": sev}
        for sev, group in (("error", report.errors), ("warning", report.warnings))
        for i in group
        if i.code != "keyed_fetch"  # an advisory of the publish report, not a mapping problem
    ]
    return MappingCheck(ok=report.valid, issues=issues, sample_columns=columns)


# ============================================================================ node config
def validate_query_shape(
    query: Any,  # Any: JSON (pipeline node config / agent tool arguments)
    spec: Mapping[str, Any],
    report: Report,
    path: str,
    node_id: str | None = None,
    *,
    max_depth: int = 3,
) -> None:
    """Structural check of an ontology query against a published spec (object type, selected
    properties, relationships, nesting depth, syntax of filters). Authorization is NOT decided
    here: it is decided per principal when the query runs."""
    types = {t["key"]: t for t in spec.get("object_types") or []}
    rels = {r["key"]: r for r in spec.get("relationships") or []}

    def err(p: str, code: str, message: str) -> None:
        report.error(p, code, message, node_id=node_id)

    def check_filter(p: str, source: Any) -> None:  # Any: JSON
        if source is None:
            return
        if not isinstance(source, str):
            err(p, "invalid_config", "a filter is an expression string")
            return
        try:
            expr.referenced_fields(source)
        except expr.FilterError as exc:
            err(p, "expression_invalid", str(exc))

    def walk(q: Any, p: str, otype: str, depth: int) -> None:  # Any: JSON
        ot = types[otype]
        props = {x["key"] for x in ot["properties"]}
        select = q.get("select")
        if (
            not isinstance(select, list)
            or not select
            or not all(isinstance(s, str) for s in select)
        ):
            err(f"{p}.select", "invalid_config", "select is a non-empty list of property keys")
        else:
            for s in select:
                if s not in props:
                    err(f"{p}.select", "invalid_config", f"{s!r} is not a property of {otype}")
        check_filter(f"{p}.filter", q.get("filter"))
        for n, hop in enumerate(q.get("traverse") or []):
            hp = f"{p}.traverse[{n}]"
            if depth >= max_depth:
                err(hp, "invalid_config", f"traversal is limited to {max_depth} levels")
                continue
            if not isinstance(hop, dict):
                err(hp, "invalid_config", "a traverse entry is an object")
                continue
            rel = rels.get(str(hop.get("relationship")))
            direction = hop.get("direction", "forward")
            if rel is None or direction not in ("forward", "reverse"):
                err(hp, "invalid_config", "unknown relationship or direction")
                continue
            start, target = (
                (rel["from"], rel["to"]) if direction == "forward" else (rel["to"], rel["from"])
            )
            if start != otype:
                err(hp, "invalid_config", f"{rel['key']} does not start at {otype} ({direction})")
                continue
            walk(hop, hp, target, depth + 1)

    if not isinstance(query, dict):
        err(path, "invalid_config", "query must be an object")
        return
    otype = query.get("object_type")
    if not isinstance(otype, str) or otype not in types:
        err(f"{path}.object_type", "invalid_config", "unknown object type")
        return
    limit = query.get("limit", 100)
    if not _is_int(limit) or not 1 <= limit <= 1000:
        err(f"{path}.limit", "invalid_config", "limit is 1-1000")
    walk(query, path, otype, 0)
