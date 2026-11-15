"""Execution Gate (SYSTEM_ARCHITECTURE 6.4, FR-AUTHZ-009): one function in front of every model
call, tool call and sub-agent start.

Order: cooperative checkpoint -> identity (the initiating principal is re-derived from the database:
user / membership / service account / API key must still be valid) -> permission (a FRESH PDP with
current grants, so a revocation mid-run stops the run at its next gate) -> resource state ->
policy -> limits / budget reservation. Approval is decided by the caller (it needs the step).
Every decision is written to `policy_decisions` (allow and deny, with run and trace ids) and every
denial raises a typed `GateDenied`, which fails the run without retry. Policy is code, never an
instruction to the model.

Every check runs for the run's own client (`ctx.tenant_id`, the run row's): the decision rows, the
events and the budget hold name it, and the pipeline lookup of a child run names it (B-2115).
A budget hold needs the call's output ceiling (R3 Item 9): a reservation without one is refused
before anything is held, since no hold could cover what the call may use.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from shifde.authz import service as authz
from shifde.authz.service import ExecutionDenied, ResourceRef
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.ids import uuid7
from shifde.ledger.estimate import MissingOutputCeiling, call_hold_tokens
from shifde.runtime import repository as repo
from shifde.runtime import state
from shifde.runtime.context import RunContext
from shifde.runtime.governance import BudgetDenied, BudgetScope, Reservation, ReserveDetail

Problem = tuple[str, str]  # (reason code, message)
Check = Callable[[], Problem | None]


@dataclass
class GateResult:
    principal: Principal
    reservation: Reservation | None = None


def budget_scope(ctx: RunContext) -> BudgetScope:
    run = ctx.run
    pipeline_id = run["pipeline_id"]
    if pipeline_id is None and run["root_run_id"] != run["id"]:  # a child of a pipeline run
        pipeline_id = repo.root_pipeline_id(ctx.session, ctx.tenant_id, run["root_run_id"])
    return BudgetScope(
        tenant_id=ctx.tenant_id,
        workspace_id=run["workspace_id"],
        project_id=run["project_id"],
        agent_id=run["agent_id"],
        root_run_id=run["root_run_id"],
        pipeline_id=pipeline_id,
        principal_id=run["initiated_by_principal_id"],
    )


def record_decision(
    ctx: RunContext,
    action: str,
    resource: ResourceRef | None,
    allowed: bool,
    reasons: Sequence[str],
    label: str | None = None,
    policy_ids: Sequence[UUID] = (),
) -> None:
    rid = None
    if resource is not None:
        rid = resource.resource_id or resource.project_id or resource.workspace_id
    with ctx.txn():  # lease-fenced: nothing is appended to a run this worker no longer owns
        _write_decision(ctx, action, resource, allowed, reasons, label, rid, policy_ids)


def _write_decision(
    ctx: RunContext,
    action: str,
    resource: ResourceRef | None,
    allowed: bool,
    reasons: Sequence[str],
    label: str | None,
    rid: UUID | None,
    policy_ids: Sequence[UUID] = (),
) -> None:
    run = ctx.run
    authz.insert_decisions(
        ctx.session,
        [
            {
                "id": uuid7(),
                "tenant_id": ctx.tenant_id,
                "at": utcnow(),
                "principal_id": run["initiated_by_principal_id"],
                "principal_type": run["principal_type"],
                "action": action,
                "resource_type": resource.resource_type if resource else None,
                "resource_id": str(rid or label or ctx.tenant_id),
                "decision": "allow" if allowed else "deny",
                "reason_codes": list(reasons),
                "matched_binding_ids": [],
                "policy_ids": list(policy_ids),
                "run_id": run["id"],
                "trace_id": run["trace_id"],
            }
        ],
    )
    repo.add_event(
        ctx.session,
        run,
        "gate_decision",
        details={
            "action": action,
            "decision": "allow" if allowed else "deny",
            "reasons": list(reasons),
            "resource": label,
            **({"policy_ids": [str(p) for p in policy_ids]} if policy_ids else {}),
        },
    )


def append_decision(
    ctx: RunContext,
    action: str,
    allowed: bool,
    reasons: Sequence[str],
    label: str | None = None,
    policy_ids: Sequence[UUID] = (),
) -> None:
    """Write a decision row inside the CALLER's open `ctx.txn()` (governance rules)."""
    _write_decision(ctx, action, None, allowed, reasons, label, None, policy_ids)


def _deny(
    ctx: RunContext,
    action: str,
    resource: ResourceRef | None,
    error_class: str,
    reason: str,
    message: str,
    label: str | None,
) -> state.GateDenied:
    record_decision(ctx, action, resource, False, [reason], label)
    ctx.audit(
        "run.gate_denied",
        {"action": action, "reason": reason, "resource": label},
        outcome="denied",
    )
    return state.GateDenied(error_class, message, reason)


def check(
    ctx: RunContext,
    action: str,
    resource: ResourceRef,
    *,
    label: str | None = None,
    also_require: Sequence[str] = (),
    resource_state: Check | None = None,
    policy: Check | None = None,
    reserve: tuple[str, int] | None = None,
    reserve_detail: ReserveDetail | None = None,
) -> GateResult:
    """Authorise one action of the run. Raises `GateDenied`, `Cancelled`, `TimedOut`,
    `Shutdown` or `LeaseLost`; returns the freshly derived principal (and the budget hold)."""
    ctx.checkpoint()
    # 1. identity - re-derived from the database at every gate
    try:
        principal = authz.execution_principal(ctx.session, ctx.identity)
    except ExecutionDenied as exc:
        raise _deny(ctx, action, resource, state.AUTHZ_DENIED, "identity", str(exc), label) from exc
    ctx.set_principal(principal)
    # 2. permission - current grants
    for needed in (action, *also_require):
        decision = ctx.scope.pdp.authorize(needed, resource)
        if not decision.allowed:
            raise _deny(
                ctx,
                action,
                resource,
                state.AUTHZ_DENIED,
                decision.reason,
                f"the principal lacks permission {needed}",
                label,
            )
    # 3. resource state, 4. policy
    for check_fn in (resource_state, policy):
        problem = check_fn() if check_fn is not None else None
        if problem is not None:
            raise _deny(ctx, action, resource, state.AUTHZ_DENIED, problem[0], problem[1], label)
    # 5. limits / budget
    reservation: Reservation | None = None
    if reserve is not None:
        try:  # Item 9: the hold covers input + output ceiling, so a call without one is refused
            call_hold_tokens(
                reserve_detail.est_input_tokens if reserve_detail else 0,
                reserve_detail.est_output_tokens if reserve_detail else None,
            )
        except MissingOutputCeiling as exc:
            raise _deny(
                ctx, action, resource, state.BUDGET_DENIED, "no_output_ceiling", str(exc), label
            ) from exc
        try:
            with ctx.txn():
                reservation = ctx.hooks.reserve(
                    ctx.session,
                    budget_scope(ctx),
                    ctx.run_id,
                    reserve[0],
                    reserve[1],
                    reserve_detail,
                )
        except BudgetDenied as exc:
            ids = [exc.budget_id] if exc.budget_id else []
            record_decision(ctx, action, resource, False, ["budget_denied"], label, ids)
            ctx.audit(
                "run.gate_denied",
                {
                    "action": action,
                    "reason": "budget_denied",
                    "detail": exc.message,
                    **({"policy_id": str(exc.budget_id)} if exc.budget_id else {}),
                },
                outcome="denied",
            )
            raise state.GateDenied(state.BUDGET_DENIED, exc.message, "budget_denied") from exc
    record_decision(ctx, action, resource, True, ["allowed"], label)
    return GateResult(principal, reservation)


def uuid_of(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None
