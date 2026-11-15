"""Prompt specs: validation, safe variable handling, rendering and evaluation (contract section 2,
SECURITY_MODEL 7).

Templates use `{{var}}` only: no logic, no expressions, no code execution. Substitution is a single
regex pass, so a variable's value is never re-scanned for placeholders. Variables with
`trusted: false` (the default) are inserted ONLY inside an
`<untrusted source="variable:{name}">...</untrusted>` data block whose content is HTML-escaped, so
the content can neither close the block nor smuggle markup into the instruction text.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, TypeGuard  # Any: stored JSON specs / variable values
from uuid import UUID

from shifde.build.env import Env, VersionRef
from shifde.build.issues import (
    IDENT,
    Report,
    check_json_schema,
    schema_errors,
)

VARIABLE_TYPES = ("string", "number", "boolean", "json", "text")
PLACEMENTS = ("system_prefix", "context_prefix")
CRITERION_TYPES = ("contains", "not_contains", "regex", "json_schema", "max_length")
SECTION_KEYS = ("system", "context", "constraints", "output_format")
KNOWN_KEYS = frozenset(
    {
        "sections",
        "variables",
        "examples",
        "composed_of",
        "default_model_id",
        "evaluation_criteria",
    }
)
MAX_SECTION_CHARS = 100_000
MAX_VALUE_CHARS = 100_000
MAX_COMPOSITION_DEPTH = 5
MAX_REGEX_CHARS = 300
_TOKEN = re.compile(r"(\\\{\{)|\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|(\{\{)")
# nested quantifiers such as (a+)+ or (a*){2,}: classic catastrophic-backtracking shapes
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,\d*\})")


# ------------------------------------------------------------------ templates
def scan_template(text: str) -> tuple[list[str], int]:
    """(placeholder names in order, number of malformed `{{` openers)."""
    names: list[str] = []
    bad = 0
    for match in _TOKEN.finditer(text):
        if match.group(2):
            names.append(match.group(2))
        elif match.group(3):
            bad += 1
    return names, bad


def escape_untrusted(value: str) -> str:
    return html.escape(value, quote=False)


def untrusted_block(name: str, value: str) -> str:
    return f'<untrusted source="variable:{name}">{escape_untrusted(value)}</untrusted>'


def substitute(text: str, values: dict[str, str]) -> str:
    """Single-pass `{{var}}` substitution; a backslash before `{{` yields a literal `{{`."""

    def repl(match: re.Match[str]) -> str:
        if match.group(1):
            return "{{"
        if match.group(2):
            return values[match.group(2)]
        return match.group(0)

    return _TOKEN.sub(repl, text)


# ------------------------------------------------------------------ spec validation
def _is_obj(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _str_section(report: Report, path: str, value: Any, *, required: bool) -> str:
    if value is None or value == "":
        if required:
            report.error(path, "required", "the system section is required")
        return ""
    if not isinstance(value, str):
        report.error(path, "invalid_type", "must be a string")
        return ""
    if len(value) > MAX_SECTION_CHARS:
        report.error(path, "too_long", f"must be at most {MAX_SECTION_CHARS} characters")
        return ""
    return value


def declared_variables(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    raw = spec.get("variables")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                out.setdefault(item["name"], item)
    return out


def validate_prompt(spec: dict[str, Any], env: Env, report: Report) -> dict[str, Any]:
    """Validate a prompt spec; returns the spec to store (composed references stay bare ids)."""
    for key in spec:
        if key not in KNOWN_KEYS:
            report.warn(key, "unknown_field", "field is ignored")

    sections = spec.get("sections")
    texts: dict[str, str] = {}
    if not _is_obj(sections):
        report.error("sections", "required", "sections.system is required")
    else:
        for key in sections:
            if key not in SECTION_KEYS:
                report.warn(f"sections.{key}", "unknown_field", "section is ignored")
        for key in SECTION_KEYS:
            texts[key] = _str_section(
                report, f"sections.{key}", sections.get(key), required=key == "system"
            )

    variables = _validate_variables(spec.get("variables"), report)
    used: set[str] = set()
    for key, text in texts.items():
        names, bad = scan_template(text)
        if bad:
            report.error(
                f"sections.{key}",
                "invalid_template",
                "only {{name}} placeholders are allowed (use \\{{ for a literal '{{')",
            )
        for name in dict.fromkeys(names):
            used.add(name)
            if name not in variables:
                report.error(
                    f"sections.{key}",
                    "undeclared_variable",
                    f"{{{{{name}}}}} is used but not declared in variables",
                )
    if not spec.get("composed_of"):
        for name in variables:
            if name not in used:
                report.warn(f"variables.{name}", "unused_variable", "declared but never used")

    _check_system_trust(spec, env, report)
    _validate_examples(spec.get("examples"), variables, report)
    _validate_criteria(spec.get("evaluation_criteria"), report)
    _validate_composition(spec.get("composed_of"), variables, env, report)

    model_id = spec.get("default_model_id")
    if model_id is not None:
        parsed = _uuid(model_id)
        if parsed is None:
            report.error("default_model_id", "invalid_value", "must be a model id")
        else:
            problem = env.model_problems([parsed]).get(parsed)
            if problem:
                report.error("default_model_id", problem, "the model cannot be used at this scope")
    return spec


def system_names(node: PromptNode) -> set[str]:
    """Placeholders that end up in the SYSTEM message: the node's own system section plus the
    system sections of its system_prefix parts (recursively)."""
    names = set(scan_template(_section(node.spec, "system"))[0])
    for placement, child in node.parts:
        if placement == "system_prefix":
            names |= system_names(child)
    return names


def untrusted_in_system(node: PromptNode) -> list[str]:
    """Variables that would put untrusted content at system-message authority (SECURITY_MODEL
    7.1): a variable is untrusted unless it is declared `trusted: true`."""
    declared = merged_variables(node)
    return sorted(
        n for n in system_names(node) if n in declared and declared[n].get("trusted") is not True
    )


def _check_system_trust(spec: dict[str, Any], env: Env, report: Report) -> None:
    try:
        node = build_node(spec, env)
    except CompositionError:
        return  # reported by the composition checks
    for name in untrusted_in_system(node):
        report.error(
            "sections.system",
            "untrusted_in_system",
            f"variable {name!r} is untrusted and cannot appear in the system message "
            "(use it in context/constraints, or declare it trusted)",
        )


def _validate_variables(raw: Any, report: Report) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if raw is None:
        return out
    if not isinstance(raw, list):
        report.error("variables", "invalid_type", "must be a list")
        return out
    for i, item in enumerate(raw):
        path = f"variables[{i}]"
        if not _is_obj(item):
            report.error(path, "invalid_type", "must be an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not IDENT.match(name):
            report.error(
                f"{path}.name", "invalid_value", "must be an identifier (letters, digits, _)"
            )
            continue
        if name in out:
            report.error(f"{path}.name", "duplicate", f"variable {name!r} is declared twice")
            continue
        out[name] = item
        vtype = item.get("type", "string")
        if vtype not in VARIABLE_TYPES:
            report.error(
                f"{path}.type", "invalid_value", f"must be one of {', '.join(VARIABLE_TYPES)}"
            )
            continue
        if "required" in item and not isinstance(item["required"], bool):
            report.error(f"{path}.required", "invalid_type", "must be a boolean")
        if "trusted" in item and not isinstance(item["trusted"], bool):
            report.error(f"{path}.trusted", "invalid_type", "must be a boolean")
        elif item.get("trusted") is True:
            report.warn(
                f"{path}.trusted",
                "trusted_variable",
                "trusted values are inserted verbatim, outside an <untrusted> block",
            )
        max_length = item.get("max_length")
        if max_length is not None and (
            isinstance(max_length, bool)
            or not isinstance(max_length, int)
            or not 1 <= max_length <= MAX_VALUE_CHARS
            or vtype not in ("string", "text")
        ):
            report.error(
                f"{path}.max_length", "invalid_value", "a positive integer, string/text only"
            )
        schema = item.get("json_schema")
        schema_ok = True
        if schema is not None:
            if vtype != "json":
                report.error(f"{path}.json_schema", "invalid_value", "only valid for type json")
                schema_ok = False
            else:
                schema_ok = check_json_schema(report, f"{path}.json_schema", schema)
        if item.get("default") is not None and schema_ok:
            problem = _coerce(item, item["default"])[1]
            if problem:
                report.error(f"{path}.default", "invalid_value", f"default {problem}")
    return out


def _validate_examples(raw: Any, variables: dict[str, dict[str, Any]], report: Report) -> None:
    if raw is None:
        return
    if not isinstance(raw, list):
        report.error("examples", "invalid_type", "must be a list")
        return
    for i, item in enumerate(raw):
        path = f"examples[{i}]"
        if (
            not _is_obj(item)
            or not _is_obj(item.get("inputs"))
            or not isinstance(item.get("output"), str)
        ):
            report.error(path, "invalid_value", "needs an object `inputs` and a string `output`")
            continue
        for key in item["inputs"]:
            if key not in variables:
                report.warn(f"{path}.inputs.{key}", "unknown_variable", "not a declared variable")


def _validate_criteria(raw: Any, report: Report) -> None:
    if raw is None:
        return
    if not isinstance(raw, list):
        report.error("evaluation_criteria", "invalid_type", "must be a list")
        return
    names: set[str] = set()
    for i, item in enumerate(raw):
        path = f"evaluation_criteria[{i}]"
        if not _is_obj(item):
            report.error(path, "invalid_type", "must be an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            report.error(f"{path}.name", "required", "a name is required")
        elif name in names:
            report.error(f"{path}.name", "duplicate", f"criterion {name!r} is declared twice")
        else:
            names.add(name)
        ctype = item.get("type")
        value = item.get("value")
        vpath = f"{path}.value"
        if ctype not in CRITERION_TYPES:
            report.error(
                f"{path}.type", "invalid_value", f"must be one of {', '.join(CRITERION_TYPES)}"
            )
        elif ctype in ("contains", "not_contains"):
            if not isinstance(value, str) or not value:
                report.error(vpath, "invalid_value", "must be a non-empty string")
        elif ctype == "regex":
            _check_regex(report, vpath, value)
        elif ctype == "max_length":
            number = _as_int(value)
            if number is None or number < 1:
                report.error(vpath, "invalid_value", "must be a positive integer")
        elif ctype == "json_schema":
            schema = _criterion_schema(value)
            if schema is None:
                report.error(vpath, "invalid_value", "must be a JSON Schema (object or JSON text)")
            else:
                check_json_schema(report, vpath, schema)


def _check_regex(report: Report, path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        report.error(path, "invalid_value", "must be a non-empty pattern")
        return
    if len(value) > MAX_REGEX_CHARS:
        report.error(path, "too_long", f"must be at most {MAX_REGEX_CHARS} characters")
        return
    if _NESTED_QUANTIFIER.search(value):
        report.error(path, "unsafe_regex", "nested quantifiers can backtrack catastrophically")
        return
    try:
        re.compile(value)
    except re.error as exc:
        report.error(path, "invalid_value", f"not a valid regular expression: {exc}")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _criterion_schema(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _validate_composition(
    raw: Any, variables: dict[str, dict[str, Any]], env: Env, report: Report
) -> None:
    if raw is None:
        return
    if not isinstance(raw, list):
        report.error("composed_of", "invalid_type", "must be a list")
        return
    seen: set[UUID] = set()
    for i, item in enumerate(raw):
        path = f"composed_of[{i}]"
        if not _is_obj(item):
            report.error(path, "invalid_type", "must be an object")
            continue
        if item.get("placement") not in PLACEMENTS:
            report.error(
                f"{path}.placement", "invalid_value", f"must be one of {', '.join(PLACEMENTS)}"
            )
        vid = _uuid(item.get("prompt_version_id"))
        if vid is None:
            report.error(f"{path}.prompt_version_id", "invalid_value", "must be a version id")
            continue
        if vid in seen:
            report.error(f"{path}.prompt_version_id", "duplicate", "composed twice")
        seen.add(vid)
        child = check_published_ref(
            report, f"{path}.prompt_version_id", env.version(vid), "prompts", env.reveal_names
        )
        if child is None:
            continue
        for name, decl in declared_variables(child.spec).items():
            mine = variables.get(name)
            if mine is not None and mine.get("type", "string") != decl.get("type", "string"):
                report.error(
                    f"{path}.prompt_version_id",
                    "variable_type_conflict",
                    f"variable {name!r} has a different type in the composed prompt",
                )


def check_published_ref(
    report: Report, path: str, found: VersionRef | None, kind: str, reveal: bool = True
) -> VersionRef | None:
    """Common reference rules: resolvable and visible (one non-revealing code for both), of the
    right kind, published (deprecated only warns), artifact neither retired nor disabled."""
    if found is None:
        report.error(path, "unresolved_reference", "no such version is available to you")
        return None
    if found.kind != kind:
        report.error(path, "wrong_kind", f"expected a {kind[:-1]} version")
        return None
    who = f"{found.artifact_key} v{found.version_no}" if reveal else "a referenced version"
    if found.state == "draft":
        report.error(path, "reference_not_published", "only published versions can be referenced")
        return None
    if found.state == "retired" or found.artifact_status == "retired":
        report.error(path, "reference_retired", f"{who} is retired")
        return None
    if found.artifact_status == "disabled":
        report.error(path, "reference_disabled", f"{who} is disabled")
        return None
    if found.state == "deprecated":
        report.warn(path, "reference_deprecated", f"{who} is deprecated")
    return found


# ------------------------------------------------------------------ variable values
def _coerce(decl: dict[str, Any], value: Any) -> tuple[Any, str | None]:
    """(coerced value, problem). Strict: no implicit conversions between types."""
    vtype = decl.get("type", "string")
    if vtype in ("string", "text"):
        if not isinstance(value, str):
            return None, "must be a string"
        limit = min(int(decl.get("max_length") or MAX_VALUE_CHARS), MAX_VALUE_CHARS)
        if len(value) > limit:
            return None, f"must be at most {limit} characters"
        return value, None
    if vtype == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None, "must be a number"
        return value, None
    if vtype == "boolean":
        return (value, None) if isinstance(value, bool) else (None, "must be a boolean")
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None, "must be JSON"
    if len(text) > MAX_VALUE_CHARS:
        return None, f"must be at most {MAX_VALUE_CHARS} characters of JSON"
    schema = decl.get("json_schema")
    if isinstance(schema, dict):
        problems = schema_errors(schema, value)
        if problems:
            return None, f"does not match its json_schema: {problems[0][1]}"
    return value, None


def _text_of(decl: dict[str, Any], value: Any) -> str:
    vtype = decl.get("type", "string")
    if vtype in ("string", "text"):
        return str(value)
    if vtype == "boolean":
        return "true" if value else "false"
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class VariableError:
    name: str
    code: str
    message: str


def resolve_variables(
    declared: dict[str, dict[str, Any]], provided: dict[str, Any]
) -> tuple[dict[str, Any], list[VariableError]]:
    """Validate supplied variables against the declarations (defaults applied)."""
    errors: list[VariableError] = []
    values: dict[str, Any] = {}
    for name in provided:
        if name not in declared:
            errors.append(VariableError(name, "unknown_variable", "is not a declared variable"))
    for name, decl in declared.items():
        if name in provided and provided[name] is not None:
            value, problem = _coerce(decl, provided[name])
            if problem:
                errors.append(VariableError(name, "invalid_value", problem))
            else:
                values[name] = value
        elif decl.get("default") is not None:
            value, problem = _coerce(decl, decl["default"])
            if problem is None:
                values[name] = value
        elif decl.get("required", True):
            errors.append(VariableError(name, "required", "is required"))
    return values, errors


# ------------------------------------------------------------------ rendering
@dataclass
class PromptNode:
    """A prompt version plus the composed prompt versions it pulls in (already loaded)."""

    spec: dict[str, Any]
    parts: list[tuple[str, PromptNode]] = field(default_factory=list)


class CompositionError(ValueError):
    """A composed prompt version is missing, not published, too deep or part of a cycle."""


def build_node(
    spec: dict[str, Any], env: Env, depth: int = 0, seen: frozenset[UUID] = frozenset()
) -> PromptNode:
    """Load the composition tree of a prompt spec through `env` (visibility-checked)."""
    if depth > MAX_COMPOSITION_DEPTH:
        raise CompositionError(f"prompt composition is nested deeper than {MAX_COMPOSITION_DEPTH}")
    parts: list[tuple[str, PromptNode]] = []
    raw = spec.get("composed_of")
    for item in raw if isinstance(raw, list) else []:
        vid = _uuid(item.get("prompt_version_id")) if isinstance(item, dict) else None
        if vid is None or item.get("placement") not in PLACEMENTS:
            raise CompositionError("composed_of has an invalid entry")
        if vid in seen:
            raise CompositionError("prompt composition contains a cycle")
        child = env.version(vid)
        if child is None or child.kind != "prompts" or child.state == "draft":
            raise CompositionError("a composed prompt version is not available")
        parts.append((item["placement"], build_node(child.spec, env, depth + 1, seen | {vid})))
    return PromptNode(spec, parts)


def merged_variables(node: PromptNode) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for _, child in node.parts:
        out.update(merged_variables(child))
    out.update(declared_variables(node.spec))  # the composing prompt's declaration wins
    return out


def _section(spec: dict[str, Any], key: str) -> str:
    sections = spec.get("sections")
    value = sections.get(key) if isinstance(sections, dict) else None
    return value if isinstance(value, str) else ""


def render_prompt(
    node: PromptNode, values: dict[str, Any], declared: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, str]], list[str]]:
    """The two-message rendering: system (role + composed system parts) and the task instruction
    (context / constraints / output format). Returns (messages, warnings)."""
    texts: dict[str, str] = {}
    for name, decl in declared.items():
        if name in values:
            raw = _text_of(decl, values[name])
            texts[name] = raw if decl.get("trusted") is True else untrusted_block(name, raw)
        else:
            texts[name] = ""  # optional variable that was not supplied

    def contribute(n: PromptNode) -> tuple[list[str], list[str]]:
        system: list[str] = []
        instruction: list[str] = []
        for placement, child in n.parts:
            child_system, child_instruction = contribute(child)
            if placement == "system_prefix":
                system.extend(child_system)
            else:
                instruction.extend(child_instruction)
        own_system = substitute(_section(n.spec, "system"), texts)
        if own_system.strip():
            system.append(own_system)
        blocks = []
        for key, label in (
            ("context", "Context"),
            ("constraints", "Constraints"),
            ("output_format", "Output format"),
        ):
            text = substitute(_section(n.spec, key), texts)
            if text.strip():
                blocks.append(f"{label}:\n{text}")
        if blocks:
            instruction.append("\n\n".join(blocks))
        return system, instruction

    system_parts, instruction_parts = contribute(node)
    messages: list[dict[str, str]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    if instruction_parts:
        messages.append({"role": "user", "content": "\n\n".join(instruction_parts)})
    warnings = [
        f"variable {name!r} is trusted: its value is inserted verbatim"
        for name, decl in declared.items()
        if decl.get("trusted") is True and name in values
    ]
    return messages, warnings


# ------------------------------------------------------------------ evaluation
@dataclass(frozen=True)
class CriterionResult:
    name: str
    passed: bool
    detail: str | None


def evaluate(criteria: list[dict[str, Any]], output: str) -> list[CriterionResult]:
    results: list[CriterionResult] = []
    for crit in criteria:
        name = str(crit.get("name", ""))
        ctype = crit.get("type")
        value = crit.get("value")
        if ctype == "contains":
            ok = isinstance(value, str) and value in output
            results.append(CriterionResult(name, ok, None if ok else f"output lacks {value!r}"))
        elif ctype == "not_contains":
            ok = isinstance(value, str) and value not in output
            results.append(CriterionResult(name, ok, None if ok else f"output contains {value!r}"))
        elif ctype == "regex":
            probe = Report()
            _check_regex(probe, "value", value)
            if probe.errors:
                results.append(CriterionResult(name, False, "the pattern is not allowed"))
                continue
            ok = re.search(str(value), output[:MAX_VALUE_CHARS]) is not None
            results.append(CriterionResult(name, ok, None if ok else "pattern did not match"))
        elif ctype == "max_length":
            limit = _as_int(value) or 0
            ok = len(output) <= limit
            results.append(
                CriterionResult(name, ok, None if ok else f"{len(output)} > {limit} characters")
            )
        elif ctype == "json_schema":
            results.append(_eval_schema(name, value, output))
        else:
            results.append(CriterionResult(name, False, "unknown criterion type"))
    return results


def _eval_schema(name: str, value: Any, output: str) -> CriterionResult:
    schema = _criterion_schema(value)
    if schema is None:
        return CriterionResult(name, False, "the schema is not valid")
    try:
        instance = json.loads(output)
    except ValueError:
        return CriterionResult(name, False, "output is not valid JSON")
    problems = schema_errors(schema, instance)
    if problems:
        return CriterionResult(name, False, problems[0][1][:300])
    return CriterionResult(name, True, None)
