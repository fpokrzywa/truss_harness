"""The deterministic Databricks App source of a sealed release (SP5 design 6.3).

`render(manifest, release_digest, resolved) -> dict[str, bytes]` is pure: no clock, randomness,
environment, network or database, and it reads only the package data under
`shifde.appgen/templates` (the "appgen.render is pure" import contract). It refuses a manifest of
another generator version with `GeneratorVersionUnsupported`, and renders the app bundle: the
managed `databricks.yml`, `app/app.yaml`, the generated service (`app/app.py`,
`app/agent_loop.py`) and its hashed lock, the four config files, the committed React bundle under
`app/static/` and `release.json`. `Resolved` lives here, not in `appgen.schemas` (which re-exports
it), because `appgen.schemas` imports `evaluation` and `packaging`, which render may not reach.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any  # Any: JSON documents
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from shifde.core.errors import AppError

GENERATOR_VERSION = "shifde.appgen/1.0"

# yaml.safe_dump(source_bundle(DatabricksApplicationSpec()), sort_keys=False) of SP2's template
DATABRICKS_YML = b"""bundle:
  name: shifde-app
  databricks_cli_version: =1.25.0
resources:
  apps:
    app:
      name: shifde-app
      source_code_path: ./app
targets:
  dev: {}
"""

Strict = ConfigDict(extra="forbid")


class GeneratorVersionUnsupported(AppError):
    status, code, title = 409, "generator_version_unsupported", "Conflict"


class ResolvedLimits(BaseModel):
    model_config = Strict
    max_iterations: int
    max_tool_calls: int
    max_tokens: int
    deadline_s: int


class ResolvedAgent(BaseModel):
    model_config = Strict
    version_id: UUID
    spec_hash: str
    name: str
    instructions: str
    limits: ResolvedLimits


class ResolvedParameter(BaseModel):
    model_config = Strict
    name: str
    type: str
    required: bool


class ResolvedTool(BaseModel):
    model_config = Strict
    version_id: UUID
    spec_hash: str
    name: str
    description: str
    sql: str
    parameters: list[ResolvedParameter]
    input_schema: dict[str, Any]
    max_rows: int


class ResolvedModel(BaseModel):
    model_config = Strict
    serving_endpoint: str


class Resolved(BaseModel):
    """The pinned versions of a `/2` release as the app needs them (design 6.3)."""

    model_config = Strict
    agent: ResolvedAgent
    tools: list[ResolvedTool]
    model: ResolvedModel
    screens: list[dict[str, Any]]


def _json(value: Any) -> bytes:  # Any: a JSON value
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _tree(node: Traversable, prefix: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        path = f"{prefix}{child.name}"
        if child.is_dir():
            out |= _tree(child, path + "/")
        else:
            out[path] = child.read_bytes()
    return out


def render(
    manifest: Mapping[str, Any], release_digest: str, resolved: Resolved | Mapping[str, Any]
) -> dict[str, bytes]:  # Any: the release manifest
    """The app source files of a `/2` release, keyed by relative POSIX path."""
    app = manifest.get("app")
    if not isinstance(app, Mapping) or app.get("generator_version") != GENERATOR_VERSION:
        raise GeneratorVersionUnsupported(
            "the release was sealed for another app generator version; seal it again"
        )
    spec = resolved if isinstance(resolved, Resolved) else Resolved.model_validate(resolved)
    templates = files("shifde.appgen").joinpath("templates")
    tables = sorted(
        {
            str(table).lower()
            for read in manifest.get("data_reads") or []
            for table in read.get("tables") or []
        }
    )
    agent = {
        "name": spec.agent.name,
        "instructions": spec.agent.instructions,
        "serving_endpoint": spec.model.serving_endpoint,
        "generator_version": GENERATOR_VERSION,
        "release_digest": release_digest,
        "tables": tables,
    }
    tools = [
        {
            "name": t.name,
            "description": t.description,
            "sql": t.sql,
            "parameters": [p.model_dump() for p in t.parameters],
            "input_schema": t.input_schema,
            "max_rows": t.max_rows,
        }
        for t in sorted(spec.tools, key=lambda t: t.name)
    ]
    limits = spec.agent.limits
    result = {
        "databricks.yml": DATABRICKS_YML,
        "app/app.yaml": templates.joinpath("app.yaml.txt").read_bytes(),
        "app/app.py": templates.joinpath("app.py.txt").read_bytes(),
        "app/agent_loop.py": templates.joinpath("agent_loop.py.txt").read_bytes(),
        "app/requirements.txt": templates.joinpath("requirements.txt").read_bytes(),
        "app/config/agent.json": _json(agent),
        "app/config/tools.json": _json(tools),
        "app/config/screens.json": _json(spec.screens),
        "app/config/limits.json": _json(
            {
                "max_iterations": limits.max_iterations,
                "max_tool_calls": limits.max_tool_calls,
                "deadline_s": limits.deadline_s,
            }
        ),
        "release.json": _json({"manifest": dict(manifest), "digest": release_digest}),
    }
    result |= _tree(templates.joinpath("web"), "app/static/")
    return result
