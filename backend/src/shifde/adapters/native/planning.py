"""Plan sealing, plan validation and the default change policy (FDE SDK `shifde_core`)."""

from __future__ import annotations

from datetime import datetime

from shifde.adapters.native.contracts import DeploymentPlan, PlanStep, Target, digest, utcnow


def bindings_digest(target: Target) -> str:
    return digest(target)


def seal_plan(plan: DeploymentPlan) -> DeploymentPlan:
    payload = plan.model_dump(mode="json", exclude={"digest"})
    return plan.model_copy(update={"digest": digest(payload)})


def validate_plan(
    plan: DeploymentPlan,
    target: Target,
    *,
    now: datetime | None = None,
    check_start_ttl: bool = True,
) -> None:
    if seal_plan(plan).digest != plan.digest:
        raise ValueError("plan digest mismatch")
    if plan.tenant_id != target.tenant_id:
        raise PermissionError("plan belongs to another tenant")
    if plan.target_id != target.id or plan.target_revision != target.revision:
        raise ValueError("target binding changed")
    if plan.bindings_digest != bindings_digest(target):
        raise ValueError("environment binding changed")
    if check_start_ttl and (now or utcnow()) >= plan.expires_at:
        raise ValueError("deployment plan expired")


def validate_changes(steps: list[PlanStep]) -> None:
    if not steps:
        raise ValueError("deployment plan must contain steps")
    if any(step.action in ("delete", "unknown") for step in steps):
        raise PermissionError("default policy forbids deletion and unknown effects")
