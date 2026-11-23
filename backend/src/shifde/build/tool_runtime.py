"""Tool execution for the P4 test panel (contract section 3).

Connection-backed executors run ONLY through `ConnectionService.open_for_use`: the connection's
status gates, `connection:use` (+ `connection:write` for write classes), the EgressGuard (with the
tool's timeout as the call deadline) and secret redaction all apply, so a tool is never a second
network path. Templates substitute arguments as *data*: path values are percent-encoded, SQL values
are bound parameters (`%(name)s`), nothing is interpolated into a statement.

On a `data_platform` (Databricks) connection, a `connection.sql` request carries the template as
`:name` parameters (`%%` as `%`), a SQL type per used parameter from the tool's input schema and the
binding's `max_rows`; which form is sent follows the type of the connection row `open_for_use`
re-read, never the spec or the arguments.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # Any: tool arguments / results
from urllib.parse import quote
from uuid import UUID

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.build.executors import BUILTIN_FUNCTIONS, WRITE_CLASSES
from shifde.build.internal_functions import INTERNAL_FUNCTIONS
from shifde.build.tools import BRACE_NAME, PARAM_NAME, named_sql
from shifde.connections.service import ConnectionService, UseSession, failure_to_error
from shifde.core.errors import ValidationFailed

RETRYABLE = frozenset(
    {ErrorClass.TIMEOUT, ErrorClass.NETWORK, ErrorClass.RATE_LIMIT, ErrorClass.UPSTREAM}
)
_MISSING = object()
Scalar = (str, int, float, bool)


@dataclass(frozen=True)
class ToolResult:
    output: Any  # Any: tool result JSON
    attempts: int


def _text(value: Any) -> str:  # Any: argument value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        import json

        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def fill(node: Any, args: dict[str, Any]) -> Any:  # Any: JSON template
    """Fill `{name}` placeholders of a JSON template. A string that is exactly one placeholder
    takes the argument's typed value; embedded placeholders are substituted as text. Entries whose
    whole value is an absent optional argument are dropped."""
    if isinstance(node, str):
        whole = BRACE_NAME.fullmatch(node)
        if whole:
            return args.get(whole.group(1), _MISSING)
        return BRACE_NAME.sub(lambda m: _text(args.get(m.group(1), "")), node)
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            filled = fill(value, args)
            if filled is not _MISSING:
                out[BRACE_NAME.sub(lambda m: _text(args.get(m.group(1), "")), key)] = filled
        return out
    if isinstance(node, list):
        return [v for v in (fill(item, args) for item in node) if v is not _MISSING]
    return node


def fill_path(template: str, args: dict[str, Any]) -> str:
    def repl(match: Any) -> str:  # Any: re.Match
        name = match.group(1)
        value = args.get(name)
        if value is None or not isinstance(value, Scalar):
            raise ValidationFailed(f"argument {name!r} is needed (as a scalar) for the path")
        segment = _text(value)
        if segment in (".", ".."):  # would climb out of the template's path prefix
            raise ValidationFailed(f"argument {name!r} cannot be a relative path segment")
        return quote(segment, safe="")

    return BRACE_NAME.sub(repl, template)


def rest_request(
    binding: dict[str, Any], args: dict[str, Any], idempotency_key: str | None
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "method": str(binding["method"]).upper(),
        "path": fill_path(str(binding["path_template"]), args),
    }
    query = binding.get("query_template")
    if query:
        filled = fill(query, args)
        request["query"] = {k: v for k, v in filled.items() if v is not None}
    body = binding.get("body_template")
    if body is not None:
        filled_body = fill(body, args)
        if filled_body is not _MISSING:
            request["body"] = filled_body
    if idempotency_key:
        request["headers"] = {"Idempotency-Key": idempotency_key}
    return request


def param_type(prop: Any) -> str:  # Any: an input schema property
    """The Databricks SQL type of an input schema property."""
    kind = prop.get("type") if isinstance(prop, dict) else None
    if kind == "integer":
        return "BIGINT"
    if kind == "number":
        return "DOUBLE"
    if kind == "boolean":
        return "BOOLEAN"
    if kind == "string" and prop.get("format") == "date":
        return "DATE"
    return "STRING"


def sql_request(
    binding: dict[str, Any],
    args: dict[str, Any],
    input_schema: dict[str, Any],
    connection_type: str,
) -> dict[str, Any]:
    template = str(binding["sql_template"])
    names = set(PARAM_NAME.findall(template))
    params = {n: args.get(n) for n in names}
    if connection_type != "data_platform":
        return {"sql": template, "params": params}
    props = input_schema.get("properties") or {}
    return {
        "sql": named_sql(template),
        "params": params,
        "param_types": {n: param_type(props.get(n)) for n in names},
        "max_rows": int(binding.get("max_rows") or 1000),
    }


def run_builtin(function_key: str, args: dict[str, Any]) -> Any:  # Any: tool result
    """Run a vetted builtin function. A `seth.*` internal function runs only inside a run (the
    runtime dispatches it to the worker's registry), so it is refused here."""
    if function_key in INTERNAL_FUNCTIONS:
        raise ValidationFailed("seth.* functions run only inside an agent run")
    return BUILTIN_FUNCTIONS[function_key].fn(args)


def execute(
    conns: ConnectionService,
    spec: dict[str, Any],
    args: dict[str, Any],
    *,
    purpose: str,
    idempotency_key: str,
    sleep: Callable[[float], None] = time.sleep,
) -> ToolResult:
    """Run a validated tool spec once (with its retry policy). Errors map to HTTP problems."""
    executor = spec["executor"]
    binding: dict[str, Any] = spec["binding"]
    if executor == "builtin.function":
        return ToolResult(run_builtin(binding["function_key"], args), 1)

    side = spec["side_effect_class"]
    write = side in WRITE_CLASSES
    retry = spec.get("retry") or {}
    max_attempts = int(retry.get("max_attempts", 1))
    if side in ("write_non_idempotent", "destructive"):
        max_attempts = 1  # never repeat a side effect automatically (also if a spec says otherwise)
    backoff = float(retry.get("backoff_s", 0))
    started = time.monotonic()
    timeout = float(spec["timeout_s"])
    connection_id = UUID(str(binding["connection_id"]))
    key = idempotency_key if spec.get("idempotency") == "key_supported" else None
    attempts = 0
    while True:
        attempts += 1
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise failure_to_error(AdapterFailure(ErrorClass.TIMEOUT, "the tool timed out"))
        try:
            with conns.open_for_use(
                connection_id, purpose, write=write, deadline=remaining
            ) as session:
                output = _invoke(spec, args, session, write, key)
            return ToolResult(output, attempts)
        except AdapterFailure as exc:
            left = timeout - (time.monotonic() - started)
            if exc.error_class not in RETRYABLE or attempts >= max_attempts or left <= 0:
                raise failure_to_error(exc) from exc
            sleep(min(backoff * (2 ** (attempts - 1)), max(left, 0)))


def _invoke(
    spec: dict[str, Any],
    args: dict[str, Any],
    session: UseSession,
    write: bool,
    key: str | None,
) -> Any:  # Any: tool result
    executor = spec["executor"]
    binding: dict[str, Any] = spec["binding"]
    if executor == "rest.http":
        result = session.invoke(rest_request(binding, args, key), write=write)
        return {**result.result, **({"truncated": True} if result.truncated else {})}
    if executor == "mcp.tool":
        result = session.invoke({"tool": binding["tool_name"], "arguments": args}, write=write)
        return result.result
    request = sql_request(binding, args, spec.get("input_schema") or {}, session.connection_type)
    result = session.invoke(request, write=write)
    out: dict[str, Any] = dict(result.result)
    max_rows = int(binding.get("max_rows") or 1000)
    rows = out.get("rows")
    if isinstance(rows, list) and len(rows) > max_rows:
        out["rows"] = rows[:max_rows]
        out["row_count"] = max_rows
        out["truncated"] = True
    elif result.truncated:
        out["truncated"] = True
    return out


def invoke_once(
    conns: ConnectionService,
    spec: dict[str, Any],
    args: dict[str, Any],
    *,
    purpose: str,
    idempotency_key: str,
    deadline: float,
    at: tuple[UUID | None, UUID | None] | None = None,
) -> Any:  # Any: tool result
    """ONE attempt of a validated tool spec, for the P5 runtime (which journals attempts, applies
    the retry policy and decides about ambiguous outcomes itself). Raises `AdapterFailure`
    unconverted so the caller can classify it; `open_for_use` errors (permissions, connection
    state, egress) are `AppError`s raised BEFORE anything is sent. `deadline` (seconds) bounds
    every network step of the call. `at` is the run's (workspace id, project id): the connection
    the session re-reads must lie there (`ConnectionService.open_for_use(at=)`)."""
    executor = spec["executor"]
    binding: dict[str, Any] = spec["binding"]
    if executor == "builtin.function":
        return run_builtin(binding["function_key"], args)
    write = spec["side_effect_class"] in WRITE_CLASSES
    key = idempotency_key if spec.get("idempotency") == "key_supported" else None
    connection_id = UUID(str(binding["connection_id"]))
    with conns.open_for_use(
        connection_id, purpose, write=write, deadline=deadline, at=at
    ) as session:
        output = _invoke(spec, args, session, write, key)
    status = output.get("status") if executor == "rest.http" and isinstance(output, dict) else None
    if isinstance(status, int) and status == 429:
        raise AdapterFailure(ErrorClass.RATE_LIMIT, "the upstream rate limited the call")
    if isinstance(status, int) and status >= 500:
        raise AdapterFailure(ErrorClass.UPSTREAM, f"the upstream answered {status}")
    return output


def refusal(executor: str, output: Any) -> str | None:  # Any: tool result
    """Why a target's ANSWER did not do the work, or None. Only a 2xx (or an MCP result without
    `isError`) is a success. `invoke_once` returns 3xx / 4xx answers (and MCP `isError` results) as
    output, which is fine for an ordinary call; the runtime asks this of a call that has an earlier
    delivery whose outcome is unknown, where an answer that is not a success cannot settle it."""
    if isinstance(output, dict):
        status = output.get("status")
        if executor == "rest.http" and isinstance(status, int) and not 200 <= status < 300:
            return f"the target answered {status}"
        if executor == "mcp.tool" and output.get("is_error") is True:
            return "the MCP tool reported an error"
    return None
