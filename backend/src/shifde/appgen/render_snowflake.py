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
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import TYPE_CHECKING, Any  # Any: JSON documents

from shifde.core.sqlrules import tokenize

if TYPE_CHECKING:
    from shifde.appgen.render import Resolved

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


def render_snowflake(
    manifest: Mapping[str, Any], release_digest: str, resolved: Resolved
) -> dict[str, bytes]:  # Any: the release manifest
    """The Native App package files of a `snowflake.native-app/v1` release, by package path."""
    templates = files("shifde.appgen").joinpath("templates")
    snowflake = templates.joinpath("snowflake")
    tables = sorted(
        {
            str(table).upper()
            for read in manifest.get("data_reads") or []
            for table in read.get("tables") or []
        }
    )
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
        sql, order = positional(t.sql)
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
