"""Skill specs (contract section 4): instructional only, never executable.

A skill is text that is added to an agent's system context at assembly. It carries no binding, no
code and no connection; `applicable_tools` is advisory and grants nothing. Validation therefore
refuses any field that looks executable, and publication refuses dependency cycles (an artifact
that, directly or through its dependencies, depends on itself).
"""

from __future__ import annotations

from typing import Any  # Any: stored JSON specs
from uuid import UUID

from shifde.build.env import Env
from shifde.build.issues import IDENT, Report
from shifde.build.prompts import VARIABLE_TYPES, check_published_ref

ALLOWED_KEYS = frozenset(
    {"instructions_md", "required_inputs", "dependencies", "applicable_tools", "validation"}
)
EXECUTABLE_KEYS = frozenset(
    {
        "command",
        "commands",
        "cmd",
        "script",
        "scripts",
        "code",
        "exec",
        "execute",
        "executor",
        "binding",
        "bindings",
        "function",
        "function_key",
        "entrypoint",
        "handler",
        "run",
        "shell",
        "python",
        "javascript",
        "sql",
        "sql_template",
        "url",
        "endpoint",
        "webhook",
        "connection_id",
        "tool_binding",
    }
)
DEFAULT_MAX_CHARS = 20_000
HARD_MAX_CHARS = 100_000
MAX_DEPTH = 20


def _find_executable(node: Any, path: str, report: Report) -> None:  # Any: JSON document
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if str(key).lower() in EXECUTABLE_KEYS:
                report.error(
                    child,
                    "executable_content_forbidden",
                    "skills are instructional only; executable fields are not allowed",
                )
            else:
                _find_executable(value, child, report)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _find_executable(item, f"{path}[{i}]", report)


def _closure_reaches(env: Env, start: UUID, target: UUID, depth: int, seen: set[UUID]) -> bool:
    """Does the dependency closure of version `start` contain an artifact `target`?"""
    if depth > MAX_DEPTH or start in seen:
        return False
    seen.add(start)
    found = env.version(start)
    if found is None:
        return False
    if found.artifact_id == target:
        return True
    for dep in _dependency_ids(found.spec):
        if _closure_reaches(env, dep, target, depth + 1, seen):
            return True
    return False


def _dependency_ids(spec: dict[str, Any]) -> list[UUID]:
    out: list[UUID] = []
    raw = spec.get("dependencies")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("skill_version_id"), str):
                try:
                    out.append(UUID(item["skill_version_id"]))
                except ValueError:
                    continue
    return out


def validate_skill(
    spec: dict[str, Any], env: Env, report: Report, self_artifact_id: UUID
) -> tuple[dict[str, Any], list[UUID]]:
    """Validate a skill spec. Returns (spec to store, dependency version ids to pin)."""
    for key in spec:
        if key not in ALLOWED_KEYS and key.lower() not in EXECUTABLE_KEYS:
            report.warn(key, "unknown_field", "field is ignored")
    _find_executable(spec, "", report)

    limit = DEFAULT_MAX_CHARS
    rules = spec.get("validation")
    if rules is not None:
        value = rules.get("max_instruction_chars") if isinstance(rules, dict) else None
        if not isinstance(rules, dict) or (
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= HARD_MAX_CHARS
            )
        ):
            report.error(
                "validation.max_instruction_chars",
                "invalid_value",
                f"must be an integer between 1 and {HARD_MAX_CHARS}",
            )
        elif value is not None:
            limit = value

    text = spec.get("instructions_md")
    if not isinstance(text, str) or not text.strip():
        report.error("instructions_md", "required", "instructions are required")
    elif len(text) > limit:
        report.error("instructions_md", "too_long", f"must be at most {limit} characters")

    _validate_inputs(spec.get("required_inputs"), report)
    tools = spec.get("applicable_tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
            report.error("applicable_tools", "invalid_type", "must be a list of tool keys")
        elif len(set(tools)) != len(tools):
            report.error("applicable_tools", "duplicate", "tool keys must be unique")
        else:
            report.warn(
                "applicable_tools",
                "advisory_only",
                "applicable_tools is advisory: it grants an agent nothing",
            )

    pins: list[UUID] = []
    deps = spec.get("dependencies")
    if deps is not None and not isinstance(deps, list):
        report.error("dependencies", "invalid_type", "must be a list")
    elif isinstance(deps, list):
        for i, item in enumerate(deps):
            path = f"dependencies[{i}].skill_version_id"
            raw = item.get("skill_version_id") if isinstance(item, dict) else None
            try:
                vid = UUID(raw) if isinstance(raw, str) else None
            except ValueError:
                vid = None
            if vid is None:
                report.error(path, "invalid_value", "must be a skill version id")
                continue
            if vid in pins:
                report.error(path, "duplicate", "dependency listed twice")
                continue
            found = check_published_ref(report, path, env.version(vid), "skills", env.reveal_names)
            if found is None:
                continue
            if found.artifact_id == self_artifact_id or _closure_reaches(
                env, vid, self_artifact_id, 0, set()
            ):
                report.error(
                    path, "dependency_cycle", "this skill would depend on itself (directly or not)"
                )
                continue
            pins.append(vid)
    return spec, pins


def _validate_inputs(raw: Any, report: Report) -> None:  # Any: JSON value
    if raw is None:
        return
    if not isinstance(raw, list):
        report.error("required_inputs", "invalid_type", "must be a list")
        return
    names: set[str] = set()
    for i, item in enumerate(raw):
        path = f"required_inputs[{i}]"
        if not isinstance(item, dict):
            report.error(path, "invalid_type", "must be an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not IDENT.match(name):
            report.error(f"{path}.name", "invalid_value", "must be an identifier")
        elif name in names:
            report.error(f"{path}.name", "duplicate", f"input {name!r} is declared twice")
        else:
            names.add(name)
        if not isinstance(item.get("description", ""), str):
            report.error(f"{path}.description", "invalid_type", "must be a string")
        if item.get("type", "string") not in VARIABLE_TYPES:
            report.error(
                f"{path}.type", "invalid_value", f"must be one of {', '.join(VARIABLE_TYPES)}"
            )


def dependency_closure(env: Env, version_ids: list[UUID]) -> list[UUID]:
    """Dependencies-first ordered closure of skill versions (each once)."""
    ordered: list[UUID] = []
    visiting: set[UUID] = set()

    def visit(vid: UUID, depth: int) -> None:
        if vid in ordered or vid in visiting or depth > MAX_DEPTH:
            return
        visiting.add(vid)
        found = env.version(vid)
        if found is not None:
            for dep in _dependency_ids(found.spec):
                visit(dep, depth + 1)
            ordered.append(vid)
        visiting.discard(vid)

    for vid in version_ids:
        visit(vid, 0)
    return ordered
