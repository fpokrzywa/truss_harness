"""Tool specs: publish-time validation (contract section 3, amendments C1 and C2).

Validation is the security boundary of a tool definition: the executor must exist and not be
`planned`, the binding must be well formed, a referenced connection must be same-tenant, active and
usable by the publisher (`connection:use`), JSON Schemas must be valid draft 2020-12 documents
without remote references, and the side-effect class must be consistent with the binding, retry
policy and idempotency declaration (SYSTEM_ARCHITECTURE 6.7 is authoritative). A `connection.sql`
tool on a Databricks or Snowflake `data_platform` connection also passes the SELECT-only statement
rules of `core/sqlrules.py`, in the connection's dialect, on its template converted to the `:name`
form. `positional_sql` gives the Snowflake form the runtime sends: `?` in order, outside literals.
"""

from __future__ import annotations

import re
from typing import Any  # Any: stored JSON specs
from uuid import UUID

from shifde.build.env import Env
from shifde.build.executors import (
    BUILTIN_FUNCTIONS,
    CONNECTION_TYPES_OF,
    DATA_PLATFORM_ADAPTER_OF,
    EXECUTORS,
    SEVERITY,
    SIDE_EFFECT_CLASSES,
    WRITE_CLASSES,
)
from shifde.build.internal_functions import INTERNAL_FUNCTIONS
from shifde.build.issues import Report, check_json_schema, schema_properties
from shifde.core import sqlrules

AUDIT_LEVELS = ("metadata", "full")
IDEMPOTENCY = ("none", "key_supported")
REST_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PERMISSION_KEY = re.compile(r"^[a-z][a-z_]*:[a-z][a-z_]*$")
BRACE_NAME = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
PARAM_NAME = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_PARAM_OR_PERCENT = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s|%%")
READ_SQL = re.compile(r"^\s*(select|with|show|values|table|explain)\b", re.I)
KNOWN_KEYS = frozenset(
    {
        "executor",
        "binding",
        "input_schema",
        "output_schema",
        "side_effect_class",
        "required_permissions",
        "invocation_policy",
        "timeout_s",
        "retry",
        "audit_level",
        "idempotency",
    }
)
MAX_TIMEOUT_S = 600
MAX_ATTEMPTS = 10
MAX_BACKOFF_S = 60


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def named_sql(template: str) -> str:
    """A stored `%(name)s` template in the data-platform form: `:name` parameters, `%%` as `%`."""
    return _PARAM_OR_PERCENT.sub(lambda m: f":{m.group(1)}" if m.group(1) else "%", template)


def _literal_end(template: str, i: int) -> int:
    """Index after the Snowflake string literal opened at `i` (`'...'` with backslash escapes, or
    `$$...$$`); the end of the template when it never closes."""
    n = len(template)
    if template.startswith("$$", i):
        end = template.find("$$", i + 2)
        return n if end < 0 else end + 2
    j = i + 1
    while j < n and template[j] != "'":
        j += 2 if template[j] == "\\" else 1
    return min(j + 1, n)


def positional_sql(template: str) -> tuple[str, list[str]]:
    """A stored `%(name)s` template in the Snowflake form: each `%(name)s` outside a string literal
    becomes `?` (the names in that order, a repeated name repeated), `%%` becomes `%`. Inside a
    literal the text is what `named_sql` gives (`:name`, never bound). A doubled `''` closes the
    literal and opens the next one, so it stays literal text."""
    out: list[str] = []
    names: list[str] = []
    i, n = 0, len(template)
    while i < n:
        if template[i] == "'" or template.startswith("$$", i):
            end = _literal_end(template, i)
            out.append(named_sql(template[i:end]))
            i = end
            continue
        m = _PARAM_OR_PERCENT.match(template, i)
        if m is None:
            out.append(template[i])
            i += 1
        elif m.group(1):
            out.append("?")
            names.append(m.group(1))
            i = m.end()
        else:
            out.append("%")
            i = m.end()
    return "".join(out), names


def template_names(node: Any, pattern: re.Pattern[str]) -> set[str]:  # Any: JSON template
    """Every placeholder name used in the strings of a JSON template."""
    if isinstance(node, str):
        return set(pattern.findall(node))
    if isinstance(node, dict):
        out: set[str] = set()
        for key, value in node.items():
            out |= template_names(key, pattern) | template_names(value, pattern)
        return out
    if isinstance(node, list):
        out = set()
        for item in node:
            out |= template_names(item, pattern)
        return out
    return set()


def validate_tool(
    spec: dict[str, Any], env: Env, report: Report, *, resolve: bool = True
) -> dict[str, Any]:
    """Validate a tool spec. With `resolve`, an `mcp.tool` binding gets its `schema_snapshot`
    (re)taken from the latest discovery; without it (re-validation of an already pinned spec) the
    stored snapshot must be present and well formed. Returns the spec to store."""
    for key in spec:
        if key not in KNOWN_KEYS:
            report.warn(key, "unknown_field", "field is ignored")
    out = dict(spec)

    executor = spec.get("executor")
    if executor not in EXECUTORS:
        report.error("executor", "invalid_value", f"must be one of {', '.join(EXECUTORS)}")
    elif _maturity(executor, spec, env) == "planned":
        report.error("executor", "executor_planned", f"executor {executor!r} is planned only")

    side = spec.get("side_effect_class")
    if side not in SIDE_EFFECT_CLASSES:
        report.error(
            "side_effect_class", "invalid_value", f"must be one of {', '.join(SIDE_EFFECT_CLASSES)}"
        )
    elif side == "destructive":
        report.warn(
            "side_effect_class",
            "destructive_tool",
            "destructive tools always require approval when an agent calls them",
        )

    input_ok = _schema(report, "input_schema", spec.get("input_schema"), must_be_object=True)
    _schema(report, "output_schema", spec.get("output_schema"), must_be_object=False)
    props = schema_properties(spec.get("input_schema")) if input_ok else set()

    _validate_policy(spec, side, report)

    binding = spec.get("binding")
    if not isinstance(binding, dict):
        report.error("binding", "required", "binding is required")
    elif executor in EXECUTORS and side in SIDE_EFFECT_CLASSES:
        out["binding"] = _validate_binding(executor, binding, side, props, env, report, resolve)
    return out


def _maturity(executor: str, spec: dict[str, Any], env: Env) -> str | None:
    """The executor's derived maturity; for `connection.sql` on a data_platform connection, that
    of the platform adapter's capability (None: an unsupported adapter, reported at the binding)."""
    binding = spec.get("binding")
    if executor == "connection.sql" and isinstance(binding, dict):
        cid = _uuid(binding.get("connection_id"))
        info = env.connection(cid) if cid is not None else None
        if info is not None and info.connection_type == "data_platform":
            platform = DATA_PLATFORM_ADAPTER_OF.get(info.adapter_key)
            return None if platform is None else env.adapter_maturity(*platform)
    return env.executor_maturity(executor)


def _schema(report: Report, path: str, schema: Any, *, must_be_object: bool) -> bool:
    if schema is None:
        report.error(path, "required", "a JSON Schema is required")
        return False
    if not check_json_schema(report, path, schema):
        return False
    if must_be_object and schema.get("type") != "object":
        report.error(path, "schema_must_be_object", 'the root schema must have "type": "object"')
        return False
    return True


def _validate_policy(spec: dict[str, Any], side: Any, report: Report) -> None:
    perms = spec.get("required_permissions", [])
    if not isinstance(perms, list) or not all(
        isinstance(p, str) and PERMISSION_KEY.match(p) for p in perms
    ):
        report.error("required_permissions", "invalid_value", "must be a list of permission keys")
    elif len(set(perms)) != len(perms):
        report.error("required_permissions", "duplicate", "permission keys must be unique")

    policy = spec.get("invocation_policy", {})
    if not isinstance(policy, dict):
        report.error("invocation_policy", "invalid_type", "must be an object")
    else:
        calls = policy.get("max_calls_per_run")
        if calls is not None and (
            isinstance(calls, bool) or not isinstance(calls, int) or calls < 1
        ):
            report.error(
                "invocation_policy.max_calls_per_run", "invalid_value", "must be a positive integer"
            )
        agents = policy.get("allowed_agent_ids")
        if agents is not None and (
            not isinstance(agents, list) or any(_uuid(a) is None for a in agents)
        ):
            report.error(
                "invocation_policy.allowed_agent_ids",
                "invalid_value",
                "must be a list of agent ids",
            )
        if "require_approval" in policy and not isinstance(policy["require_approval"], bool):
            report.error(
                "invocation_policy.require_approval", "invalid_value", "must be true or false"
            )

    timeout = spec.get("timeout_s")
    if (
        not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or not (0 < timeout <= MAX_TIMEOUT_S)
    ):
        report.error("timeout_s", "invalid_value", f"must be between 0 and {MAX_TIMEOUT_S} seconds")

    retry = spec.get("retry", {"max_attempts": 1, "backoff_s": 0})
    attempts = 1
    if not isinstance(retry, dict):
        report.error("retry", "invalid_type", "must be an object")
    else:
        raw_attempts = retry.get("max_attempts", 1)
        backoff = retry.get("backoff_s", 0)
        if (
            isinstance(raw_attempts, bool)
            or not isinstance(raw_attempts, int)
            or not 1 <= raw_attempts <= MAX_ATTEMPTS
        ):
            report.error(
                "retry.max_attempts", "invalid_value", f"must be between 1 and {MAX_ATTEMPTS}"
            )
        else:
            attempts = raw_attempts
        if not _is_number(backoff) or not 0 <= backoff <= MAX_BACKOFF_S:
            report.error(
                "retry.backoff_s", "invalid_value", f"must be between 0 and {MAX_BACKOFF_S} seconds"
            )

    if spec.get("audit_level") not in AUDIT_LEVELS:
        report.error("audit_level", "invalid_value", f"must be one of {', '.join(AUDIT_LEVELS)}")
    idempotency = spec.get("idempotency", "none")
    if idempotency not in IDEMPOTENCY:
        report.error("idempotency", "invalid_value", f"must be one of {', '.join(IDEMPOTENCY)}")

    if side in ("write_non_idempotent", "destructive") and attempts > 1:
        report.error(
            "retry.max_attempts",
            "retry_forbidden",
            f"{side} tools must not be retried automatically (max_attempts must be 1)",
        )
    if side == "write_idempotent" and idempotency != "key_supported":
        report.error(
            "idempotency",
            "idempotency_required",
            'write_idempotent tools must declare idempotency "key_supported"',
        )


def _validate_binding(
    executor: str,
    binding: dict[str, Any],
    side: str,
    props: set[str],
    env: Env,
    report: Report,
    resolve: bool,
) -> dict[str, Any]:
    out = dict(binding)
    if executor == "builtin.function":
        _allowed(report, binding, {"function_key"})
        key = binding.get("function_key")
        # a vetted builtin, or a `seth.*` internal function (runs only inside a run)
        fn = (
            BUILTIN_FUNCTIONS.get(key) or INTERNAL_FUNCTIONS.get(key)
            if isinstance(key, str)
            else None
        )
        if fn is None:
            report.error(
                "binding.function_key", "unknown_function", "not a vetted builtin function"
            )
        elif SEVERITY[side] < SEVERITY[fn.side_effect_class]:
            report.error(
                "side_effect_class",
                "side_effect_understated",
                f"function {fn.key} is {fn.side_effect_class}; a tool cannot declare less",
            )
        return out

    allowed = {
        "rest.http": {
            "connection_id",
            "method",
            "path_template",
            "query_template",
            "body_template",
        },
        "mcp.tool": {"connection_id", "tool_name", "schema_snapshot"},
        "connection.sql": {"connection_id", "sql_template", "max_rows"},
    }[executor]
    _allowed(report, binding, allowed)
    info = _connection(report, binding, executor, side, env)

    if executor == "rest.http":
        _validate_rest(binding, side, props, report)
    elif executor == "mcp.tool":
        out = _validate_mcp(binding, side, env, report, resolve, info)
    else:
        _validate_sql(binding, side, props, report)
        if info is not None and info.connection_type == "data_platform":
            _validate_platform_sql(binding, report, info.adapter_key)
    return out


def _allowed(report: Report, binding: dict[str, Any], allowed: set[str]) -> None:
    for key in binding:
        if key not in allowed:
            report.warn(f"binding.{key}", "unknown_field", "field is ignored")


def _connection(
    report: Report, binding: dict[str, Any], executor: str, side: str, env: Env
) -> Any:  # Any: ConnectionInfo | None
    cid = _uuid(binding.get("connection_id"))
    path = "binding.connection_id"
    if cid is None:
        report.error(path, "required", "a connection id is required")
        return None
    info = env.connection(cid)
    if info is None:
        report.error(path, "connection_not_found", "no such connection in this organization")
        return None
    allowed = CONNECTION_TYPES_OF[executor]
    if info.connection_type not in allowed:
        report.error(
            path,
            "wrong_connection_type",
            f"{executor} needs a {' or '.join(sorted(allowed))} connection",
        )
        return None
    if info.connection_type == "data_platform" and info.adapter_key not in DATA_PLATFORM_ADAPTER_OF:
        report.error(
            path,
            "executor_adapter_unsupported",
            "connection.sql supports Databricks and Snowflake data platform connections only",
        )
    if not info.implemented:
        report.error(path, "executor_planned", "the connection's adapter is planned only")
    if info.status != "active":
        report.error(path, "connection_not_active", f"the connection is {info.status}")
    if not info.usable_by_publisher:
        report.error(path, "connection_use_denied", "you do not hold connection:use on it")
    if side in WRITE_CLASSES and info.access_mode != "read_write":
        report.error(path, "connection_read_only", f"a {side} tool needs a read_write connection")
    return info


def _validate_rest(binding: dict[str, Any], side: str, props: set[str], report: Report) -> None:
    method = binding.get("method")
    if method not in REST_METHODS:
        report.error("binding.method", "invalid_value", f"must be one of {', '.join(REST_METHODS)}")
    elif method not in SAFE_METHODS and side not in WRITE_CLASSES:
        report.error(
            "side_effect_class",
            "write_method_requires_write_class",
            f"{method} can change data: declare a write_* or destructive class",
        )
    path_t = binding.get("path_template")
    if not isinstance(path_t, str) or not path_t.strip():
        report.error("binding.path_template", "required", "a path template is required")
    else:
        if (
            not path_t.startswith("/")
            or path_t.startswith("//")
            or "://" in path_t
            or "\\" in path_t
            or any(
                seg.lower().replace("%2e", ".") in (".", "..")
                for seg in path_t.split("?")[0].split("/")
            )
        ):
            report.error(
                "binding.path_template",
                "invalid_path",
                "must start with '/' and be a path on the connection's base URL "
                "(no scheme, host, backslash, '.' or '..' segments)",
            )
        if "?" in path_t or "#" in path_t:
            report.error("binding.path_template", "invalid_path", "use query_template for queries")
        stripped = BRACE_NAME.sub("", path_t)
        if "{" in stripped or "}" in stripped:
            report.error("binding.path_template", "invalid_template", "malformed {placeholder}")
    query_t = binding.get("query_template")
    if query_t is not None and (
        not isinstance(query_t, dict)
        or not all(isinstance(v, str | int | float | bool) or v is None for v in query_t.values())
    ):
        report.error("binding.query_template", "invalid_type", "must be an object of scalars")
    used = (
        template_names(path_t, BRACE_NAME)
        | template_names(query_t, BRACE_NAME)
        | template_names(binding.get("body_template"), BRACE_NAME)
    )
    for name in sorted(used - props):
        report.error(
            "binding", "unknown_argument", f"{{{name}}} is not a property of the input schema"
        )


def _validate_sql(binding: dict[str, Any], side: str, props: set[str], report: Report) -> None:
    sql = binding.get("sql_template")
    if not isinstance(sql, str) or not sql.strip():
        report.error("binding.sql_template", "required", "a SQL template is required")
        return
    body = sql.strip().rstrip(";").rstrip()
    if ";" in body:
        report.error("binding.sql_template", "invalid_template", "exactly one statement is allowed")
    leftover = PARAM_NAME.sub("", sql).replace("%%", "")
    if "%" in leftover:
        report.error(
            "binding.sql_template",
            "invalid_template",
            "only %(name)s bound parameters are allowed (write a literal percent as %%)",
        )
    for name in sorted(set(PARAM_NAME.findall(sql)) - props):
        report.error(
            "binding", "unknown_argument", f"%({name})s is not a property of the input schema"
        )
    if not READ_SQL.match(body) and side not in WRITE_CLASSES:
        report.error(
            "side_effect_class",
            "write_statement_requires_write_class",
            "only read statements are allowed for none/read tools",
        )
    max_rows = binding.get("max_rows")
    if max_rows is not None and (
        isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 10000
    ):
        report.error("binding.max_rows", "invalid_value", "must be between 1 and 10000")


def _validate_platform_sql(binding: dict[str, Any], report: Report, adapter_key: str) -> None:
    """The SELECT-only statement rules, in the dialect of the connection's adapter, on the
    template as the data platform receives it."""
    sql = binding.get("sql_template")
    if not isinstance(sql, str) or not sql.strip():
        return  # `_validate_sql` reported it
    after = [sql[m.end() : m.end() + 1] for m in _PARAM_OR_PERCENT.finditer(sql) if m.group(1)]
    if any(ch.isalnum() or ch == "_" for ch in after):  # `%(a)s_x` would become `:a_x`
        report.error(
            "binding.sql_template",
            "invalid_template",
            "a %(name)s parameter must not be followed by a letter, digit or underscore",
        )
    dialect: sqlrules.Dialect = "snowflake" if adapter_key == "snowflake" else "databricks"
    for issue in sqlrules.statement_rules(named_sql(sql), dialect=dialect):
        report.error("binding.sql_template", issue.code, issue.message)


def _validate_mcp(
    binding: dict[str, Any],
    side: str,
    env: Env,
    report: Report,
    resolve: bool,
    info: Any,  # Any: ConnectionInfo | None
) -> dict[str, Any]:
    out = dict(binding)
    name = binding.get("tool_name")
    if not isinstance(name, str) or not name:
        report.error("binding.tool_name", "required", "an MCP tool name is required")
        return out
    if not resolve:
        snap = binding.get("schema_snapshot")
        if (
            not isinstance(snap, dict)
            or snap.get("name") != name
            or not isinstance(snap.get("input_schema"), dict)
        ):
            report.error(
                "binding.schema_snapshot", "snapshot_missing", "the pinned snapshot is missing"
            )
        return out
    if info is None:
        return out
    found = env.mcp_tool(info.id, name)
    if not found.exists:
        report.error(
            "binding.tool_name",
            "mcp_discovery_required",
            "run tool discovery on the connection before publishing",
        )
        return out
    if found.tool is None:
        report.error(
            "binding.tool_name", "mcp_tool_not_discovered", f"the server lists no tool {name!r}"
        )
        return out
    if side not in WRITE_CLASSES and found.tool.get("read_only") is not True:
        report.error(
            "side_effect_class",
            "tool_not_read_only",
            "the MCP tool is not annotated read-only: declare a write_* or destructive class",
        )
    snapshot: dict[str, Any] = {
        "name": found.tool["name"],
        "input_schema": found.tool["input_schema"],
    }
    if found.tool.get("description"):
        snapshot["description"] = found.tool["description"]
    out["schema_snapshot"] = snapshot
    return out
