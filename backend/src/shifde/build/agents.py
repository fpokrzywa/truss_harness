"""Agent specs (contract section 5, amendment C2).

An agent version PINS its components: the prompt version, every tool version and every skill
version (and, through them, their dependencies) are stored as version ids. A draft may name an
artifact as `latest:{artifact_id}`; publishing resolves it to the artifact's latest *published*
version and stores the id, so publishing a new prompt/tool/skill version never changes an
existing published agent version (reproducibility).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any  # Any: stored JSON specs
from uuid import UUID

from shifde.build.env import Env, VersionRef
from shifde.build.executors import SIDE_EFFECT_CLASSES
from shifde.build.issues import IDENT, Report
from shifde.build.prompts import (
    CompositionError,
    build_node,
    check_published_ref,
    merged_variables,
    scan_template,
    untrusted_in_system,
)
from shifde.build.skills import dependency_closure

KNOWN_KEYS = frozenset(
    {
        "prompt_version_id",
        "model",
        "tool_version_ids",
        "skill_version_ids",
        "memory",
        "limits",
        "execution_permissions",
        "approval_rules",
        "variables_map",
    }
)
MEMORY_MODES = ("none", "run", "session")
PRINCIPAL_TYPES = ("user", "service_account")
DEFAULT_MAXIMA: dict[str, float] = {
    "max_iterations": 100,
    "max_tool_calls": 500,
    "max_tokens": 2_000_000,
    "deadline_s": 86_400,
}
LATEST = re.compile(r"^latest:([0-9a-fA-F-]{36})$")
INPUT_REF = re.compile(r"\{\{\s*input(?:\.[A-Za-z_][A-Za-z0-9_]*)*\s*\}\}")
MAX_TURNS = 1000


@dataclass
class AgentPins:
    prompt: VersionRef | None = None
    tools: list[VersionRef] = field(default_factory=list)
    skills: list[VersionRef] = field(default_factory=list)


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _resolve(
    report: Report,
    path: str,
    ref: Any,
    kind: str,
    env: Env,
    *,
    resolve: bool,
) -> VersionRef | None:
    """A version reference: a bare version id, or `latest:{artifact_id}` (when `resolve`)."""
    if not isinstance(ref, str):
        report.error(path, "invalid_value", "must be a version id or latest:{artifact_id}")
        return None
    latest = LATEST.match(ref)
    if latest is not None:
        if not resolve:
            report.error(path, "unpinned_reference", "a published agent must pin version ids")
            return None
        aid = _uuid(latest.group(1))
        if aid is None:
            report.error(path, "invalid_value", "malformed latest:{artifact_id}")
            return None
        found = env.latest(aid)
        if found is None:
            report.error(path, "unresolved_reference", "no published version is available to you")
            return None
        return check_published_ref(report, path, found, kind, env.reveal_names)
    vid = _uuid(ref)
    if vid is None:
        report.error(path, "invalid_value", "must be a version id or latest:{artifact_id}")
        return None
    return check_published_ref(report, path, env.version(vid), kind, env.reveal_names)


def validate_agent(
    spec: dict[str, Any],
    env: Env,
    report: Report,
    self_artifact_id: UUID,
    maxima: dict[str, float],
    *,
    resolve: bool = True,
) -> tuple[dict[str, Any], AgentPins]:
    """Validate an agent spec. Returns (spec with every reference pinned, the resolved pins)."""
    for key in spec:
        if key not in KNOWN_KEYS:
            report.warn(key, "unknown_field", "field is ignored")
    out: dict[str, Any] = dict(spec)
    pins = AgentPins()

    # ---- prompt
    prompt = None
    if spec.get("prompt_version_id") is None:
        report.error("prompt_version_id", "required", "a prompt is required")
    else:
        prompt = _resolve(
            report, "prompt_version_id", spec["prompt_version_id"], "prompts", env, resolve=resolve
        )
    if prompt is not None:
        pins.prompt = prompt
        out["prompt_version_id"] = str(prompt.version_id)

    # ---- tools
    tool_refs = spec.get("tool_version_ids", [])
    if not isinstance(tool_refs, list):
        report.error("tool_version_ids", "invalid_type", "must be a list")
        tool_refs = []
    tool_ids: list[str] = []
    seen_artifacts: set[UUID] = set()
    for i, ref in enumerate(tool_refs):
        path = f"tool_version_ids[{i}]"
        tool = _resolve(report, path, ref, "tools", env, resolve=resolve)
        if tool is None:
            tool_ids.append(ref if isinstance(ref, str) else "")
            continue
        if tool.artifact_id in seen_artifacts:
            report.error(
                path,
                "duplicate",
                f"tool {tool.artifact_key if env.reveal_names else '(hidden)'} is listed twice",
            )
            continue
        seen_artifacts.add(tool.artifact_id)
        policy = tool.spec.get("invocation_policy")
        allowed = policy.get("allowed_agent_ids") if isinstance(policy, dict) else None
        if isinstance(allowed, list) and str(self_artifact_id) not in {str(a) for a in allowed}:
            report.error(
                path,
                "tool_not_allowed_for_agent",
                f"tool {tool.artifact_key if env.reveal_names else '(hidden)'} limits its agents",
            )
        pins.tools.append(tool)
        tool_ids.append(str(tool.version_id))
    out["tool_version_ids"] = tool_ids

    # ---- skills
    skill_refs = spec.get("skill_version_ids", [])
    if not isinstance(skill_refs, list):
        report.error("skill_version_ids", "invalid_type", "must be a list")
        skill_refs = []
    skill_ids: list[str] = []
    seen_skills: set[UUID] = set()
    for i, ref in enumerate(skill_refs):
        path = f"skill_version_ids[{i}]"
        skill = _resolve(report, path, ref, "skills", env, resolve=resolve)
        if skill is None:
            skill_ids.append(ref if isinstance(ref, str) else "")
            continue
        if skill.artifact_id in seen_skills:
            report.error(
                path,
                "duplicate",
                f"skill {skill.artifact_key if env.reveal_names else '(hidden)'} is listed twice",
            )
            continue
        seen_skills.add(skill.artifact_id)
        pins.skills.append(skill)
        skill_ids.append(str(skill.version_id))
    out["skill_version_ids"] = skill_ids
    if pins.skills and not report.errors:
        for vid in dependency_closure(env, [s.version_id for s in pins.skills]):
            closure = env.version(vid)
            if closure is None or closure.state == "retired" or closure.artifact_status != "active":
                report.error(
                    "skill_version_ids",
                    "skill_dependency_unavailable",
                    "a skill dependency is retired, disabled or no longer visible",
                )
                break

    _validate_model(spec.get("model"), env, report)
    out["memory"] = _validate_memory(spec.get("memory"), report)
    _validate_limits(spec.get("limits"), maxima, report)
    out["execution_permissions"] = _validate_execution(spec.get("execution_permissions"), report)
    out["approval_rules"] = _validate_approval(spec.get("approval_rules"), report)
    out["variables_map"] = _validate_variables_map(
        spec.get("variables_map"), pins.prompt, env, report
    )
    return out, pins


def _validate_model(model: Any, env: Env, report: Report) -> None:
    if not isinstance(model, dict):
        report.error("model", "required", "a model or model route is required")
        return
    has_model = "model_id" in model
    has_route = "route_id" in model
    if has_model == has_route:
        report.error("model", "invalid_value", "give exactly one of model_id or route_id")
        return
    if has_route:
        route = _uuid(model["route_id"])
        ids = env.route_model_ids(route) if route is not None else None
        if ids is None:
            report.error("model.route_id", "unresolved_reference", "no such model route")
            return
        problems = env.model_problems(ids)
        for mid in ids:
            if mid in problems:
                report.error("model.route_id", problems[mid], f"route model {mid}: {problems[mid]}")
        return
    primary = _uuid(model["model_id"])
    fallbacks_raw = model.get("fallback_model_ids", [])
    if primary is None:
        report.error("model.model_id", "invalid_value", "must be a model id")
        return
    if not isinstance(fallbacks_raw, list) or any(_uuid(f) is None for f in fallbacks_raw):
        report.error("model.fallback_model_ids", "invalid_value", "must be a list of model ids")
        return
    fallbacks = [UUID(f) for f in fallbacks_raw]
    everyone = [primary, *fallbacks]
    if len(set(everyone)) != len(everyone):
        report.error("model.fallback_model_ids", "duplicate", "models must be distinct")
    problems = env.model_problems(everyone)
    if primary in problems:
        report.error("model.model_id", problems[primary], "the model cannot be used at this scope")
    for i, mid in enumerate(fallbacks):
        if mid in problems:
            report.error(
                f"model.fallback_model_ids[{i}]", problems[mid], "the model cannot be used here"
            )


def _validate_memory(memory: Any, report: Report) -> dict[str, Any]:
    if memory is None:
        return {"mode": "none", "max_turns": 0}
    if not isinstance(memory, dict):
        report.error("memory", "invalid_type", "must be an object")
        return {"mode": "none", "max_turns": 0}
    mode = memory.get("mode", "none")
    turns = memory.get("max_turns", 0)
    if mode not in MEMORY_MODES:
        report.error("memory.mode", "invalid_value", f"must be one of {', '.join(MEMORY_MODES)}")
    if isinstance(turns, bool) or not isinstance(turns, int) or not 0 <= turns <= MAX_TURNS:
        report.error("memory.max_turns", "invalid_value", f"must be between 0 and {MAX_TURNS}")
    return {"mode": mode, "max_turns": turns}


def _validate_limits(limits: Any, maxima: dict[str, float], report: Report) -> None:
    if not isinstance(limits, dict):
        report.error("limits", "required", "run limits are required")
        return
    for key, cap in maxima.items():
        value = limits.get(key)
        path = f"limits.{key}"
        if value is None:
            report.error(path, "required", "is required")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
            report.error(path, "invalid_value", "must be a positive integer")
        elif value > cap:
            report.error(path, "exceeds_maximum", f"exceeds the installation maximum of {int(cap)}")
    cost = limits.get("max_cost")
    if cost is not None and (
        isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0
    ):
        report.error("limits.max_cost", "invalid_value", "must be a non-negative number")


def _validate_execution(raw: Any, report: Report) -> dict[str, Any]:
    default = {
        "allowed_principal_types": ["user", "service_account"],
        "required_permission": "agent:execute",
    }
    if raw is None:
        return default
    if not isinstance(raw, dict):
        report.error("execution_permissions", "invalid_type", "must be an object")
        return default
    types = raw.get("allowed_principal_types", default["allowed_principal_types"])
    if (
        not isinstance(types, list)
        or not types
        or any(t not in PRINCIPAL_TYPES for t in types)
        or len(set(types)) != len(types)
    ):
        report.error(
            "execution_permissions.allowed_principal_types",
            "invalid_value",
            f"must be a non-empty subset of {', '.join(PRINCIPAL_TYPES)}",
        )
        types = default["allowed_principal_types"]
    required = raw.get("required_permission", "agent:execute")
    if required != "agent:execute":
        report.error(
            "execution_permissions.required_permission",
            "invalid_value",
            "must be agent:execute",
        )
    return {"allowed_principal_types": list(types), "required_permission": "agent:execute"}


def _validate_approval(raw: Any, report: Report) -> dict[str, Any]:
    required = ["write_non_idempotent", "destructive"]
    if raw is None:
        return {"require_approval_for": required}
    if not isinstance(raw, dict):
        report.error("approval_rules", "invalid_type", "must be an object")
        return {"require_approval_for": required}
    classes = raw.get("require_approval_for", required)
    if not isinstance(classes, list) or any(c not in SIDE_EFFECT_CLASSES for c in classes):
        report.error(
            "approval_rules.require_approval_for",
            "invalid_value",
            f"must be a list of {', '.join(SIDE_EFFECT_CLASSES)}",
        )
        return {"require_approval_for": required}
    normalized = sorted(set(classes) | {"destructive"}, key=SIDE_EFFECT_CLASSES.index)
    if "destructive" not in classes:
        report.warn(
            "approval_rules.require_approval_for",
            "destructive_forced",
            "destructive tools always require approval; it was added",
        )
    return {"require_approval_for": normalized}


def _validate_variables_map(
    raw: Any, prompt: VersionRef | None, env: Env, report: Report
) -> dict[str, str]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()
    ):
        report.error("variables_map", "invalid_type", "must map variable names to strings")
        return {}
    for name, value in raw.items():
        if not IDENT.match(name):
            report.error(f"variables_map.{name}", "invalid_value", "not a variable name")
        leftover = INPUT_REF.sub("", value)
        names, bad = scan_template(leftover)
        if names or bad or "{{" in leftover:
            report.error(
                f"variables_map.{name}",
                "invalid_template",
                "only {{input}} and {{input.field}} references are allowed",
            )
    if prompt is not None:
        try:
            declared = merged_variables(build_node(prompt.spec, env))
        except CompositionError as exc:
            report.error("prompt_version_id", "invalid_composition", str(exc))
            return dict(raw)
        for name, value in raw.items():
            if name not in declared:
                report.error(
                    f"variables_map.{name}", "unknown_variable", "not a variable of the prompt"
                )
            elif declared[name].get("trusted") is True and INPUT_REF.search(value):
                report.error(
                    f"variables_map.{name}",
                    "untrusted_into_trusted",
                    "run input is untrusted and cannot feed a trusted variable",
                )
        for name in untrusted_in_system(build_node(prompt.spec, env)):
            report.error(
                "prompt_version_id",
                "untrusted_in_system",
                f"prompt variable {name!r} is untrusted but used in the system message",
            )
        for name, decl in declared.items():
            if decl.get("required", True) and decl.get("default") is None and name not in raw:
                report.error(
                    "variables_map",
                    "unsatisfied_variable",
                    f"required prompt variable {name!r} has no mapping",
                )
    return dict(raw)
