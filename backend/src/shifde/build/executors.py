"""Tool executors: the vetted builtin function registry and the executor catalog (contract
section 3, `GET /api/v1/tool-executors`).

Only four executor kinds exist. `builtin.function` runs code that ships with the platform and is
listed in `BUILTIN_FUNCTIONS` (nothing a tenant or a model can add). The other three reach external
systems exclusively through a P3 connection (`ConnectionService.open_for_use`), so every call passes
the connection's status gates, `connection:use` / `connection:write`, the EgressGuard and secret
redaction: there is no second network path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any  # Any: JSON arguments / results

SIDE_EFFECT_CLASSES = ("none", "read", "write_idempotent", "write_non_idempotent", "destructive")
WRITE_CLASSES = frozenset({"write_idempotent", "write_non_idempotent", "destructive"})
SEVERITY = {name: i for i, name in enumerate(SIDE_EFFECT_CLASSES)}
EXECUTORS = ("builtin.function", "rest.http", "mcp.tool", "connection.sql")
# executor -> (adapter key, capability) whose derived maturity the executor inherits
ADAPTER_OF = {
    "rest.http": ("rest.http", "rest.call"),
    "mcp.tool": ("mcp_http", "mcp.tools"),
    "connection.sql": ("postgres", "query.sql"),
}
CONNECTION_TYPE_OF = {"rest.http": "rest", "mcp.tool": "mcp", "connection.sql": "database"}


@dataclass(frozen=True)
class BuiltinFunction:
    key: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect_class: str
    fn: Callable[[dict[str, Any]], dict[str, Any]]


def _echo(args: dict[str, Any]) -> dict[str, Any]:
    return {"text": args["text"]}


def _word_count(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args["text"])
    return {"words": len(text.split()), "characters": len(text)}


def _sum(args: dict[str, Any]) -> dict[str, Any]:
    return {"sum": sum(args["values"])}


def _pick(args: dict[str, Any]) -> dict[str, Any]:
    source = args["object"]
    return {"picked": {k: source[k] for k in args["keys"] if k in source}}


def _now(_: dict[str, Any]) -> dict[str, Any]:
    return {"iso": datetime.now(tz=UTC).isoformat()}


ONTOLOGY_QUERY = "ontology.query"


def _ontology_query(_: dict[str, Any]) -> dict[str, Any]:
    """Runs only inside an agent / pipeline run (it needs the run principal and the Execution
    Gate); the test panel and any other caller get a clear refusal."""
    from shifde.core.errors import ValidationFailed

    raise ValidationFailed("ontology.query runs only inside an agent or pipeline run")


_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_QUERY_SCHEMA: dict[str, Any] = {
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
        "traverse": {"type": "array", "maxItems": 8, "items": {"type": "object"}},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
    },
}
BUILTIN_FUNCTIONS: dict[str, BuiltinFunction] = {
    f.key: f
    for f in (
        BuiltinFunction(
            "util.echo",
            "Return the given text unchanged.",
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["text"],
                "additionalProperties": False,
                "properties": {"text": {"type": "string", "maxLength": 100000}},
            },
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "none",
            _echo,
        ),
        BuiltinFunction(
            "text.word_count",
            "Count the words and characters of a text.",
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["text"],
                "additionalProperties": False,
                "properties": {"text": {"type": "string", "maxLength": 1000000}},
            },
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["words", "characters"],
                "properties": {"words": {"type": "integer"}, "characters": {"type": "integer"}},
            },
            "none",
            _word_count,
        ),
        BuiltinFunction(
            "math.sum",
            "Add a list of numbers.",
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["values"],
                "additionalProperties": False,
                "properties": {
                    "values": {"type": "array", "maxItems": 10000, "items": {"type": "number"}}
                },
            },
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["sum"],
                "properties": {"sum": {"type": "number"}},
            },
            "none",
            _sum,
        ),
        BuiltinFunction(
            "json.pick",
            "Pick the named keys out of a JSON object.",
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["object", "keys"],
                "additionalProperties": False,
                "properties": {
                    "object": {"type": "object"},
                    "keys": {"type": "array", "maxItems": 1000, "items": {"type": "string"}},
                },
            },
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["picked"],
                "properties": {"picked": {"type": "object"}},
            },
            "none",
            _pick,
        ),
        BuiltinFunction(
            ONTOLOGY_QUERY,
            "Query an ontology (object type, properties, filter, relationship traversal). The "
            "calling principal ontology permissions and access policies apply; rows come back "
            "as untrusted data with provenance ontology:<object type>.",
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["ontology_id", "query"],
                "additionalProperties": False,
                "properties": {
                    "ontology_id": {"type": "string", "format": "uuid"},
                    "version_id": {"type": "string", "format": "uuid"},
                    "query": _QUERY_SCHEMA,
                },
            },
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["object_type", "rows", "lineage_id"],
                "properties": {
                    "object_type": {"type": "string"},
                    "rows": {"type": "array", "items": {"type": "object"}},
                    "row_count": {"type": "integer"},
                    "lineage_id": {"type": "string"},
                },
            },
            "read",
            _ontology_query,
        ),
        BuiltinFunction(
            "time.now_utc",
            "The current UTC time (ISO 8601).",
            {"$schema": _DRAFT, "type": "object", "additionalProperties": False, "properties": {}},
            {
                "$schema": _DRAFT,
                "type": "object",
                "required": ["iso"],
                "properties": {"iso": {"type": "string"}},
            },
            "none",
            _now,
        ),
    )
}


def binding_schema(executor: str) -> dict[str, Any]:
    """JSON Schema of the `binding` object of each executor (drives the editor's binding form)."""
    conn = {"type": "string", "format": "uuid", "title": "Connection"}
    if executor == "builtin.function":
        return {
            "$schema": _DRAFT,
            "type": "object",
            "required": ["function_key"],
            "additionalProperties": False,
            "properties": {"function_key": {"type": "string", "enum": sorted(BUILTIN_FUNCTIONS)}},
        }
    if executor == "rest.http":
        return {
            "$schema": _DRAFT,
            "type": "object",
            "required": ["connection_id", "method", "path_template"],
            "additionalProperties": False,
            "properties": {
                "connection_id": conn,
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                },
                "path_template": {"type": "string", "description": "Path with {arg} placeholders"},
                "query_template": {"type": "object"},
                "body_template": {},
            },
        }
    if executor == "mcp.tool":
        return {
            "$schema": _DRAFT,
            "type": "object",
            "required": ["connection_id", "tool_name"],
            "additionalProperties": False,
            "properties": {
                "connection_id": conn,
                "tool_name": {"type": "string"},
                "schema_snapshot": {
                    "type": "object",
                    "readOnly": True,
                    "description": "Set by the server at publish from the latest discovery",
                },
            },
        }
    return {
        "$schema": _DRAFT,
        "type": "object",
        "required": ["connection_id", "sql_template"],
        "additionalProperties": False,
        "properties": {
            "connection_id": conn,
            "sql_template": {
                "type": "string",
                "description": "One statement with %(arg)s bound parameters",
            },
            "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
        },
    }
