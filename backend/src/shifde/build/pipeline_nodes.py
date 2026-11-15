"""The pipeline node-type registry (docs/api/PHASE_6_CONTRACT.md section 2, no fake nodes).

Each entry declares a node type's category, config JSON Schema and typed ports. A type is listed
`enabled` only when its executor exists in `shifde.runtime.pipeline` AND, for the adapter-backed
types, the adapter capability it depends on is not `planned`; maturity is derived exactly like the
P3/P4 executors (declared adapter + recorded conformance evidence), never stored. Types a later
phase adds (`ontology_query`, P7) register through `register()`; an entry whose executor is not
implemented is declared `planned` and is never executable.

Port schemas are JSON Schema subsets (see `compatible`): validation rule 4 checks every
output -> input binding structurally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any  # Any: JSON Schema documents

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
VERSION_REF_PATTERN = (
    r"^(latest:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
IDENT_PATH = r"^[A-Za-z_][A-Za-z0-9_]{0,62}(\.[A-Za-z_][A-Za-z0-9_]{0,62})?$"
BINDING = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,63})\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
PARAM_BINDING = re.compile(r"^\$params\.([A-Za-z_][A-Za-z0-9_]{0,63})$")

ANY: dict[str, Any] = {}
OBJECT: dict[str, Any] = {"type": "object"}
ROWS: dict[str, Any] = {"type": "array", "items": {"type": "object"}}
TEXT: dict[str, Any] = {"type": "string"}
INTEGER: dict[str, Any] = {"type": "integer"}
BOOLEAN: dict[str, Any] = {"type": "boolean"}

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TRANSFORM_OPS = ("select", "rename", "filter", "map", "cast", "aggregate")
AGGREGATE_FNS = ("count", "sum", "avg", "min", "max")
CAST_TYPES = ("string", "number", "integer", "boolean")


@dataclass(frozen=True)
class Port:
    name: str
    schema: dict[str, Any]
    required: bool = True


@dataclass(frozen=True)
class NodeType:
    type: str
    label: str
    category: str
    config_schema: dict[str, Any]
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]
    side_effect: bool = False  # the type CAN have side effects (decided per node, see validation)
    dynamic_inputs: bool = False  # input ports are named by the node (placeholders / expression)
    executor: str | None = None  # adapter-backed executor whose maturity the type inherits
    implemented: bool = True
    reason_disabled: str | None = None
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def input(self, name: str) -> Port | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output(self, name: str) -> Port | None:
        return next((p for p in self.outputs if p.name == name), None)


def _obj(
    properties: dict[str, Any], required: list[str] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
        **extra,
    }


CONNECTION = {"type": "string", "pattern": UUID_PATTERN, "title": "Connection"}
TABLE = {
    "type": "string",
    "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,62}(\.[A-Za-z_][A-Za-z0-9_]{0,62})?$",
    "title": "Table (schema.table)",
}

_REGISTRY: dict[str, NodeType] = {}


def register(node_type: NodeType) -> None:
    """Add a node type (a later phase's module registers its own; keys are unique)."""
    if node_type.type in _REGISTRY:
        raise ValueError(f"node type {node_type.type!r} is already registered")
    _REGISTRY[node_type.type] = node_type


def get(type_: str) -> NodeType | None:
    return _REGISTRY.get(type_)


def all_types() -> list[NodeType]:
    return list(_REGISTRY.values())


register(
    NodeType(
        "data_source",
        "Data source",
        "source",
        _obj(
            {
                "connection_id": CONNECTION,
                "dataset": TABLE,
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
            },
            ["connection_id", "dataset"],
        ),
        (),
        (Port("rows", ROWS), Port("row_count", INTEGER)),
        executor="connection.sql",
        description="Read-only rows of a table of an approved database connection.",
    )
)
register(
    NodeType(
        "db_query",
        "Database query",
        "source",
        _obj(
            {
                "connection_id": CONNECTION,
                "sql": {"type": "string", "minLength": 1, "maxLength": 20000},
                "args": {"type": "object", "default": {}},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
            },
            ["connection_id", "sql"],
        ),
        (),
        (Port("rows", ROWS), Port("row_count", INTEGER)),
        executor="connection.sql",
        description="Parameterized read-only SQL (named %(arg)s placeholders bound as data).",
    )
)
register(
    NodeType(
        "ontology_query",
        "Ontology query",
        "source",
        _obj(
            {
                "ontology_id": {"type": "string", "pattern": UUID_PATTERN, "title": "Ontology"},
                "version_id": {"type": "string", "pattern": VERSION_REF_PATTERN},
                "query": {
                    "type": "object",
                    "required": ["object_type", "select"],
                    "additionalProperties": False,
                    "properties": {
                        "object_type": {"type": "string", "minLength": 1, "maxLength": 200},
                        "select": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 200,
                            "items": {"type": "string", "maxLength": 100},
                        },
                        "filter": {"type": "string", "maxLength": 1000},
                        "traverse": {"type": "array", "maxItems": 8, "items": OBJECT},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
            },
            ["ontology_id", "version_id", "query"],
        ),
        (),
        (Port("rows", ROWS), Port("row_count", INTEGER), Port("lineage_id", TEXT)),
        executor="connection.sql",
        description="Query a published ontology version as the run principal: access "
        "policies, row filters and the Execution Gate apply; filters may read `params.<name>`.",
    )
)
register(
    NodeType(
        "api_request",
        "API request",
        "action",
        _obj(
            {
                "connection_id": CONNECTION,
                "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
                "path_template": {"type": "string", "minLength": 1, "maxLength": 2000},
                "query": {"type": "object"},
                "body": {},
            },
            ["connection_id", "path_template"],
        ),
        (),
        (Port("response", OBJECT), Port("status", INTEGER)),
        side_effect=True,
        dynamic_inputs=True,
        executor="rest.http",
        description="REST call; `{name}` placeholders are filled from the named input ports.",
    )
)
register(
    NodeType(
        "validate",
        "Validate",
        "logic",
        _obj(
            {
                "schema": OBJECT,
                "on_invalid": {"enum": ["fail", "drop", "route"], "default": "fail"},
            },
            ["schema"],
        ),
        (Port("data", ANY),),
        (Port("data", ANY), Port("invalid", ROWS, required=False)),
        description="JSON Schema check of a document, or of every row of a list.",
    )
)
register(
    NodeType(
        "transform",
        "Transform",
        "logic",
        _obj(
            {"operations": {"type": "array", "minItems": 1, "maxItems": 50, "items": OBJECT}},
            ["operations"],
        ),
        (Port("data", {"type": ["array", "object"]}),),
        (Port("data", ANY),),
        description="Restricted operators: select, rename, filter, map, cast, aggregate.",
    )
)
register(
    NodeType(
        "condition",
        "Condition",
        "control",
        _obj({"expression": {"type": "string", "minLength": 1, "maxLength": 1000}}, ["expression"]),
        (),
        (Port("result", BOOLEAN),),
        dynamic_inputs=True,
        description="Safe expression over `inputs.<port>` and `params.<name>`; edges use when.",
    )
)
register(
    NodeType(
        "model",
        "Model",
        "ai",
        _obj(
            {
                "model_id": {"type": "string", "pattern": UUID_PATTERN, "title": "Model"},
                "system": {"type": "string", "maxLength": 20000},
            },
            ["model_id"],
        ),
        (Port("prompt", ANY),),
        (Port("text", TEXT),),
        description="One governed model call; the input is passed as untrusted data.",
    )
)
register(
    NodeType(
        "prompt",
        "Prompt",
        "ai",
        _obj(
            {
                "prompt_version_id": {"type": "string", "pattern": VERSION_REF_PATTERN},
                "model_id": {"type": "string", "pattern": UUID_PATTERN, "title": "Model"},
            },
            ["prompt_version_id", "model_id"],
        ),
        (Port("variables", OBJECT, required=False),),
        (Port("text", TEXT),),
        description="Renders a published prompt with the input variables and calls the model.",
    )
)
register(
    NodeType(
        "agent",
        "Agent",
        "ai",
        _obj(
            {"agent_version_id": {"type": "string", "pattern": VERSION_REF_PATTERN}},
            ["agent_version_id"],
        ),
        (Port("input", ANY), Port("variables", OBJECT, required=False)),
        (Port("output", TEXT),),
        description="Runs a published agent version as a child run.",
    )
)
register(
    NodeType(
        "tool",
        "Tool",
        "action",
        _obj(
            {"tool_version_id": {"type": "string", "pattern": VERSION_REF_PATTERN}},
            ["tool_version_id"],
        ),
        (Port("args", OBJECT),),
        (Port("output", ANY),),
        side_effect=True,
        description="Invokes a published tool version through the Execution Gate.",
    )
)
register(
    NodeType(
        "approval",
        "Approval",
        "control",
        _obj(
            {
                "approver_permission": {"const": "approval:decide", "default": "approval:decide"},
                "expires_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 604800,
                    "default": 86400,
                },
                "message": {"type": "string", "maxLength": 1000},
            }
        ),
        (Port("context", ANY, required=False),),
        (Port("approved", ANY),),
        description="Pauses the run until a person other than the initiator approves.",
    )
)
register(
    NodeType(
        "sink",
        "Sink",
        "action",
        _obj(
            {
                "target": {"enum": ["run_artifact", "connection"]},
                "mode": {"enum": ["append", "replace"], "default": "replace"},
                "connection_id": CONNECTION,
                "method": {"enum": ["POST", "PUT"], "default": "POST"},
                "path_template": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            ["target"],
        ),
        (Port("document", ANY),),
        (Port("result", OBJECT),),
        side_effect=True,
        executor="rest.http",
        description="Stores the result as a run artifact, or delivers it to a REST connection.",
    )
)

NODE_TYPE_ORDER = (
    "data_source",
    "api_request",
    "db_query",
    "ontology_query",
    "validate",
    "transform",
    "condition",
    "model",
    "prompt",
    "agent",
    "tool",
    "approval",
    "sink",
)


# ------------------------------------------------------------------ schema compatibility
def _types(schema: dict[str, Any]) -> set[str] | None:
    raw = schema.get("type")
    if raw is None:
        return None
    return set(raw) if isinstance(raw, list) else {str(raw)}


def _props(schema: dict[str, Any]) -> dict[str, Any]:
    raw = schema.get("properties")
    return raw if isinstance(raw, dict) else {}


def compatible(src: dict[str, Any], dst: dict[str, Any], depth: int = 0) -> str | None:
    """None when a value described by `src` is always accepted where `dst` is declared, else the
    reason. A schema without a type is `any` (accepted by everything, and accepting everything).
    Structural subset: types (integer is a number), object `required` / `properties`, array
    `items`."""
    if depth > 8 or not src or not dst:
        return None
    s_types, d_types = _types(src), _types(dst)
    if s_types is not None and d_types is not None:
        accepted = all(t in d_types or (t == "integer" and "number" in d_types) for t in s_types)
        if not accepted:
            return f"{'/'.join(sorted(s_types))} is not accepted where {'/'.join(sorted(d_types))}"
    s_props = _props(src)
    d_props = _props(dst)
    for name in dst.get("required") or []:
        if s_props and name not in s_props and name not in (src.get("required") or []):
            return f"the required field {name!r} is missing"
    for name, sub in d_props.items():
        if name in s_props and isinstance(sub, dict) and isinstance(s_props[name], dict):
            reason = compatible(s_props[name], sub, depth + 1)
            if reason:
                return f"{name}: {reason}"
    s_items, d_items = src.get("items"), dst.get("items")
    if isinstance(s_items, dict) and isinstance(d_items, dict):
        reason = compatible(s_items, d_items, depth + 1)
        if reason:
            return f"items: {reason}"
    return None


def expand_refs(schema: Any, named: dict[str, Any], depth: int = 0) -> Any:  # Any: JSON Schema
    """Inline `{"$ref": "#/schemas/Name"}` references to the pipeline's `schemas` map."""
    if depth > 6:
        return schema
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/schemas/"):
            target = named.get(ref[len("#/schemas/") :])
            return expand_refs(target, named, depth + 1) if isinstance(target, dict) else {}
        return {k: expand_refs(v, named, depth + 1) for k, v in schema.items()}
    if isinstance(schema, list):
        return [expand_refs(v, named, depth + 1) for v in schema]
    return schema
