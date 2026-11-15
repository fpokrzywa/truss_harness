"""Server-side validation of governance policy definitions (Amendment G1) and retention floors."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any  # Any: JSON documents
from uuid import UUID

from shifde.core.errors import ValidationFailed

# the tool side-effect classes of build.executors.SIDE_EFFECT_CLASSES (governance sits below build
# in the module layering, so the names are repeated here; a test keeps them in step)
SIDE_EFFECT_CLASSES = frozenset(
    {"none", "read", "write_idempotent", "write_non_idempotent", "destructive"}
)
ARTIFACT_KINDS = frozenset(
    {"prompts", "tools", "skills", "agents", "orchestrations", "pipelines", "ontologies"}
)

# Platform minimums (SECURITY_MODEL / PRD OQ-16: audit floor 90 days) and defaults.
RETENTION_FLOORS: dict[str, int] = {
    "audit_events": 90,
    "usage_events": 30,
    "policy_decisions": 30,
    "run_artifacts": 1,
    "run_steps_content": 1,
}
RETENTION_DEFAULTS: dict[str, int] = {
    "run_artifacts": 90,
    "run_steps_content": 90,
    "audit_events": 365,
    "usage_events": 395,
    "policy_decisions": 365,
}

# What the platform actually does with each kind (`PolicyOut.enforcement`).
_ENFORCEMENT = {
    "model_policy": "enforced",  # written through to the P3 allowlist the model gate reads
    "approval_rule": "enforced",  # tool calls of agent / orchestration / ontology-action runs
    "cost_threshold": "enforced",  # alert, or approval before the next tool call
    "data_export": "enforced",  # usage and audit exports
    "lifecycle": "not_enforced",  # stored and reported; no maintenance job evaluates it yet
}


def enforcement_of(kind: str) -> str:
    return _ENFORCEMENT.get(kind, "not_enforced")


def _fail(message: str) -> ValidationFailed:
    return ValidationFailed(message)


def _positive(value: object, what: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _fail(f"{what} must be a number") from exc
    if not number.is_finite() or number <= 0:
        raise _fail(f"{what} must be greater than 0")
    return number


def _currency(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise _fail(f"{what} must be a 3-letter upper-case currency code")
    return value


def _only(definition: Mapping[str, Any], allowed: Collection[str], kind: str) -> None:  # Any: JSON
    extra = sorted(set(definition) - set(allowed))
    if extra:
        raise _fail(f"unknown {kind} field(s): {', '.join(extra)}")


def validate_definition(
    kind: str,
    definition: Mapping[str, Any],  # Any: JSON document
    *,
    known_permissions: Collection[str],
) -> dict[str, Any]:  # Any: normalized JSON document
    """Return the normalized definition or raise 422."""
    if kind == "cost_threshold":
        _only(definition, {"threshold_amount", "currency", "action"}, kind)
        action = definition.get("action", "alert")
        if action not in ("alert", "require_approval"):
            raise _fail("action must be alert or require_approval")
        return {
            "threshold_amount": str(
                _positive(definition.get("threshold_amount"), "threshold_amount")
            ),
            "currency": _currency(definition.get("currency"), "currency"),
            "action": action,
        }
    if kind == "lifecycle":
        _only(definition, {"artifact_kinds", "deprecate_unused_after_days"}, kind)
        kinds = definition.get("artifact_kinds")
        if not isinstance(kinds, list) or not kinds or not set(kinds) <= ARTIFACT_KINDS:
            raise _fail(
                f"artifact_kinds must be a non-empty list of: {', '.join(sorted(ARTIFACT_KINDS))}"
            )
        days = definition.get("deprecate_unused_after_days")
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650:
            raise _fail("deprecate_unused_after_days must be an integer between 1 and 3650")
        return {"artifact_kinds": sorted(set(kinds)), "deprecate_unused_after_days": days}
    if kind == "approval_rule":
        _only(definition, {"when", "approver_permission"}, kind)
        when = definition.get("when")
        if not isinstance(when, dict) or not when:
            raise _fail("when must name side_effect_classes and / or estimated_cost_gt")
        _only(when, {"side_effect_classes", "estimated_cost_gt", "currency"}, "approval_rule.when")
        out_when: dict[str, Any] = {}  # Any: JSON
        if "side_effect_classes" in when:
            classes = when["side_effect_classes"]
            if (
                not isinstance(classes, list)
                or not classes
                or not set(classes) <= SIDE_EFFECT_CLASSES
            ):
                raise _fail(
                    "side_effect_classes must be a non-empty list of: "
                    + ", ".join(sorted(SIDE_EFFECT_CLASSES))
                )
            out_when["side_effect_classes"] = sorted(set(classes))
        if "estimated_cost_gt" in when:
            out_when["estimated_cost_gt"] = str(
                _positive(when["estimated_cost_gt"], "estimated_cost_gt")
            )
            out_when["currency"] = _currency(when.get("currency"), "currency")
        elif "currency" in when:
            raise _fail("currency only applies with estimated_cost_gt")
        if not out_when:
            raise _fail("when must name side_effect_classes and / or estimated_cost_gt")
        out: dict[str, Any] = {"when": out_when}  # Any: JSON
        permission = definition.get("approver_permission")
        if permission is not None:
            if not isinstance(permission, str) or permission not in known_permissions:
                raise _fail("approver_permission must be an existing permission key")
            out["approver_permission"] = permission
        return out
    if kind == "data_export":
        _only(definition, {"allow_export", "max_rows"}, kind)
        out = {}
        if "allow_export" in definition:
            if not isinstance(definition["allow_export"], bool):
                raise _fail("allow_export must be true or false")
            out["allow_export"] = definition["allow_export"]
        if "max_rows" in definition:
            rows = definition["max_rows"]
            if not isinstance(rows, int) or isinstance(rows, bool) or not 1 <= rows <= 1_000_000:
                raise _fail("max_rows must be an integer between 1 and 1000000")
            out["max_rows"] = rows
        if not out:
            raise _fail("a data_export policy needs allow_export and / or max_rows")
        return out
    if kind == "model_policy":
        _only(definition, {"allowed_model_ids", "enabled"}, kind)
        ids = definition.get("allowed_model_ids", [])
        if not isinstance(ids, list) or len(ids) > 1000:
            raise _fail("allowed_model_ids must be a list of model ids")
        try:
            parsed = sorted({str(UUID(str(i))) for i in ids})
        except ValueError as exc:
            raise _fail("allowed_model_ids must be UUIDs") from exc
        enabled = definition.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _fail("enabled must be true or false")
        return {"allowed_model_ids": parsed, "enabled": enabled}
    raise _fail(f"unknown policy kind: {kind}")
