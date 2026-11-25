"""The deterministic Snowflake Native App package of a sealed release (SP6 design 3 and 6.3).

`render_snowflake(manifest, release_digest, resolved) -> dict[str, bytes]` is pure, like
`appgen.render` (which dispatches a `snowflake.native-app/v1` release here after checking its
generator version): it reads only the package data under `shifde.appgen/templates/snowflake` and
the platform-neutral `templates/agent_loop.py.txt` (the "appgen.render is pure" import contract).
The package is the fixed `manifest.yml`, `setup.sql` and `ui/environment.yml`, the Streamlit app
(`ui/streamlit_app.py`, `ui/agent_loop.py` byte for byte the Databricks app's,
`ui/backend_snowflake.py`), the deep-health handler `ui/deep_health.py`, the four config files
under `ui/config/` and `release.json`. `agent.json` names the release's single model binding as
`cortex_model` and the data-read tables uppercased; each `tools.json` entry carries the tool's
`:name` SQL with every parameter as `?` and the names in occurrence order (`param_order`).

A release the app may not run is refused before any byte is rendered (`ReleaseNotRenderable`, 409
`release_not_renderable`): a data-read table that is not `database.schema.table` of plain names
(deep health puts it into SQL that runs under the application owner's rights), or a tool whose SQL
is longer than a draft's may be, breaks the Snowflake statement rules, uses a `:name` the tool does
not declare or breaks the Snowflake table rules over the release's data reads (#358: anything but a
declared table, a CTE or a subquery in a FROM clause, any other three-part name, any
`IDENTIFIER(...)` or `TABLE(...)`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import TYPE_CHECKING, Any  # Any: JSON documents

from shifde.core.errors import AppError
from shifde.core.sqlrules import (
    MAX_TOOL_SQL_CHARS,
    statement_rules,
    table_name_supported,
    table_rules,
    tokenize,
)

if TYPE_CHECKING:
    from shifde.appgen.render import Resolved, ResolvedTool

TARGET_KIND = "snowflake.native-app/v1"
GENERATOR_VERSION = "shifde.appgen.snowflake/1.0"
# package path -> template file name under `templates/snowflake/`
FIXED = {
    "manifest.yml": "manifest.yml.txt",
    "setup.sql": "setup.sql.txt",
    "ui/environment.yml": "environment.yml",
    "ui/streamlit_app.py": "streamlit_app.py.txt",
    "ui/backend_snowflake.py": "backend_snowflake.py.txt",
    "ui/deep_health.py": "deep_health.py.txt",
}


class ReleaseNotRenderable(AppError):
    status, code, title = 409, "release_not_renderable", "Conflict"


def _json(value: Any) -> bytes:  # Any: a JSON value
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def positional(sql: str) -> tuple[str, list[str]]:
    """`sql` with each `:name` parameter outside a string literal as `?`, and the names in
    occurrence order (a name used twice is listed twice)."""
    out: list[str] = []
    order: list[str] = []
    last = 0
    for token in tokenize(sql, dialect="snowflake"):
        if token.kind == "param":
            out += [sql[last : token.start], "?"]
            order.append(token.value)
            last = token.end
    out.append(sql[last:])
    return "".join(out), order


def _tables(manifest: Mapping[str, Any]) -> list[str]:  # Any: the release manifest
    """The data-read tables uppercased, de-duplicated and sorted. The names are checked as written:
    uppercasing first would turn some non-ASCII letters into plain ones."""
    tables: set[str] = set()
    for read in manifest.get("data_reads") or []:
        for value in read.get("tables") or []:
            table = str(value)
            if table.count(".") != 2 or not table_name_supported(table, "snowflake"):
                raise ReleaseNotRenderable(
                    f"the data-read table {table!r} is not database.schema.table of plain names"
                )
            tables.add(table.upper())
    return sorted(tables)


def _tool_sql(tool: ResolvedTool, tables: list[str]) -> tuple[str, list[str]]:
    """`positional` of the tool's SQL; refused unless it is no longer than a draft's may be (a
    hand-published tool's may; checked before any rule, since rendering runs in the API process),
    keeps the Snowflake statement rules (a tool published before them may not), uses only declared
    parameters (SETH binds `%(name)s`, so another `:name` is SQL text there, not a parameter) and
    keeps `table_rules` over `tables`, the data reads the plan's approver sees. Those rules fail
    closed on FROM clauses but read no object a function names (`table_rules`): the statement rules
    refuse `get_ddl`, `get_presigned_url` and `nextval` by name, and the installed app's grants
    limit the rest."""
    refused = f"the SQL of tool {tool.name!r} is refused"
    if len(tool.sql) > MAX_TOOL_SQL_CHARS:
        raise ReleaseNotRenderable(f"{refused}: it is longer than {MAX_TOOL_SQL_CHARS} characters")
    issues = [issue.message for issue in statement_rules(tool.sql, dialect="snowflake")]
    sql, order = positional(tool.sql)
    declared = {p.name for p in tool.parameters}
    issues += [f":{n} is not a declared parameter" for n in order if n not in declared]
    if not issues:
        issues = [issue.message for issue in table_rules(tool.sql, tables, dialect="snowflake")]
    if issues:
        raise ReleaseNotRenderable(f"{refused}: {issues[0]}")
    return sql, order


def render_snowflake(
    manifest: Mapping[str, Any], release_digest: str, resolved: Resolved
) -> dict[str, bytes]:  # Any: the release manifest
    """The Native App package files of a `snowflake.native-app/v1` release, by package path."""
    templates = files("shifde.appgen").joinpath("templates")
    snowflake = templates.joinpath("snowflake")
    tables = _tables(manifest)
    agent = {
        "name": resolved.agent.name,
        "instructions": resolved.agent.instructions,
        "cortex_model": resolved.model.serving_endpoint,
        "generator_version": GENERATOR_VERSION,
        "release_digest": release_digest,
        "tables": tables,
    }
    tools = []
    for t in sorted(resolved.tools, key=lambda t: t.name):
        sql, order = _tool_sql(t, tables)
        tools.append(
            {
                "name": t.name,
                "description": t.description,
                "sql": sql,
                "param_order": order,
                "parameters": [p.model_dump() for p in t.parameters],
                "input_schema": t.input_schema,
                "max_rows": t.max_rows,
            }
        )
    limits = resolved.agent.limits
    result = {path: snowflake.joinpath(name).read_bytes() for path, name in FIXED.items()}
    result |= {
        "ui/agent_loop.py": templates.joinpath("agent_loop.py.txt").read_bytes(),
        "ui/config/agent.json": _json(agent),
        "ui/config/tools.json": _json(tools),
        "ui/config/screens.json": _json(resolved.screens),
        "ui/config/limits.json": _json(
            {
                "max_iterations": limits.max_iterations,
                "max_tool_calls": limits.max_tool_calls,
                "deadline_s": limits.deadline_s,
            }
        ),
        "release.json": _json({"manifest": dict(manifest), "digest": release_digest}),
    }
    return dict(sorted(result.items()))
