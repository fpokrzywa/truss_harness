"""Configurable governance policies evaluated inside a run (FR-GOV-004/005).

* `approval_rule` (`when.side_effect_classes`, `when.estimated_cost_gt` + `when.currency`,
  `approver_permission`): a tool call needs a human approval. A rule can only ADD an approval - it
  never waives the `destructive` / write-class / rule-8 approvals the platform already requires -
  and the approval then needs the rule's `approver_permission` on top of `approval:decide`;
  separation of duties and the indeterminate-side-effect flow are unchanged.
* `cost_threshold` (`threshold_amount`, `currency`, `action`): when the known cost of the run tree
  exceeds the threshold the run raises an alert (`governance.cost_threshold` audit event and a
  policy decision, once per run and policy); with action `require_approval` every following tool
  call needs approval. Model calls are not interrupted by this policy (budgets do that).

A policy applies to a run when its scope is empty (the whole tenant) or names one of the run's
tenant / workspace / project / agent / pipeline / principal. "Estimated cost" means the cost
KNOWN so far for the run tree (calculated or provider-reported); unmeasured calls contribute
nothing, which the alert says explicitly.

Only the run's own client's policies apply: every statement names the run's `tenant_id` next to
row-level security (B-2130), so another client's rule, cost or alert never reaches this run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any  # Any: policy definition documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.ledger.enforcement import scope_match, scope_params
from shifde.runtime import gate
from shifde.runtime.context import RunContext

if TYPE_CHECKING:
    from shifde.runtime.steps import ToolCall


@dataclass(frozen=True)
class ApprovalRule:
    policy_ids: tuple[UUID, ...]
    approver_permission: str | None


def _policies(ctx: RunContext, kind: str) -> list[dict[str, Any]]:  # Any: policy rows
    scope = gate.budget_scope(ctx)
    rows = ctx.session.execute(
        text(
            "SELECT p.id, p.kind, p.scope_type, p.scope_id, p.definition "
            "FROM shifde.governance_policies p WHERE p.tenant_id = :tenant "
            "AND p.status = 'active' AND p.kind = :kind "
            f"AND (p.scope_type IS NULL OR {scope_match('p')}) ORDER BY p.id"
        ),
        {"kind": kind, **scope_params(scope)},
    ).mappings()
    return [dict(r) for r in rows]


def run_cost(session: Session, ctx: RunContext, currency: str) -> Decimal:
    """Known cost of the run tree in `currency` (events of the root run and its children)."""
    return Decimal(
        session.execute(
            text(
                "SELECT COALESCE(sum(cost_amount), 0) FROM shifde.usage_events "
                "WHERE tenant_id = :t AND root_run_id = :root AND currency = :cur"
            ),
            {"t": ctx.tenant_id, "root": ctx.run["root_run_id"], "cur": currency},
        ).scalar_one()
    )


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _rule_matches(ctx: RunContext, when: Mapping[str, Any], side: str) -> bool:  # Any: JSON
    classes = when.get("side_effect_classes")
    has_cost = "estimated_cost_gt" in when
    if classes is None and not has_cost:
        return False  # validation refuses such a rule; ignore a hand-edited one
    if classes is not None and side not in classes:
        return False
    if has_cost:
        threshold = _decimal(when.get("estimated_cost_gt"))
        if threshold is None or run_cost(ctx.session, ctx, str(when.get("currency"))) <= threshold:
            return False
    return True


def tool_approval(ctx: RunContext, call: ToolCall, side: str) -> ApprovalRule | None:
    """The governance approval a tool call needs, if any (None = no rule applies)."""
    matched: list[UUID] = []
    permission: str | None = None
    for pol in _policies(ctx, "approval_rule"):
        d: Mapping[str, Any] = pol["definition"] or {}
        if _rule_matches(ctx, d.get("when") or {}, side):
            matched.append(pol["id"])
            permission = permission or d.get("approver_permission")
    for pol in _policies(ctx, "cost_threshold"):
        d = pol["definition"] or {}
        limit = _decimal(d.get("threshold_amount"))
        if (
            d.get("action") == "require_approval"
            and limit is not None
            and run_cost(ctx.session, ctx, str(d.get("currency"))) > limit
        ):
            matched.append(pol["id"])
    if not matched:
        return None
    return ApprovalRule(tuple(matched), permission)


def record_rule_decision(
    ctx: RunContext, action: str, policy_ids: Sequence[UUID], reason: str
) -> None:
    """A decision row for a governance rule, written in the caller's open transaction."""
    gate.append_decision(ctx, action, False, [reason], "governance", policy_ids)


def after_model_call(ctx: RunContext, event: ledger.EventResult) -> None:
    """Alert when the run tree's known cost crossed a `cost_threshold` policy (once per policy)."""
    if event.costed.amount is None or event.costed.currency is None:
        return
    for pol in _policies(ctx, "cost_threshold"):
        d: Mapping[str, Any] = pol["definition"] or {}
        limit = _decimal(d.get("threshold_amount"))
        currency = str(d.get("currency") or "")
        if limit is None or currency != event.costed.currency:
            continue
        cost = run_cost(ctx.session, ctx, currency)
        ctx.session.commit()
        if cost <= limit or _already_alerted(ctx, pol["id"]):
            continue
        with ctx.txn():
            gate.append_decision(
                ctx,
                "governance.cost_threshold",
                True,
                ["cost_threshold"],
                "governance",
                [pol["id"]],
            )
            ctx.audit(
                "governance.cost_threshold",
                {
                    "policy_id": str(pol["id"]),
                    "action": d.get("action"),
                    "threshold_amount": str(limit),
                    "currency": currency,
                    "known_cost": str(cost),
                    "note": "known cost only; unmeasured calls are not included",
                },
                commit=False,
            )


def _already_alerted(ctx: RunContext, policy_id: UUID) -> bool:
    row = ctx.session.execute(
        text(
            "SELECT 1 FROM shifde.policy_decisions WHERE tenant_id = :t AND run_id = :r "
            "AND :p = ANY (policy_ids) AND 'cost_threshold' = ANY (reason_codes) LIMIT 1"
        ),
        {"t": ctx.tenant_id, "r": ctx.run_id, "p": policy_id},
    ).first()
    ctx.session.commit()
    return row is not None
