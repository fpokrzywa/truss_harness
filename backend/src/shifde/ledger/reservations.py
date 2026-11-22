"""Budget holds (`shifde.budget_reservations`): every statement that writes one lives here, shared
by the run gate (`runtime.governance`) and direct model invocations (`models_admin`).

A hold belongs to a run step (`run_id`, `step_key`) or to one direct invocation
(`invocation_id`). `provider_request_at` is the provider-request marker (design 12.2): set just
before the provider call may start, it means the call may have reached the provider. Such a hold
is never silently freed: it does not expire after its TTL, the end of its run does not release it
(`settle_ended_run`, runtime_0004), and an orphan handler turns it into `settled_unknown` at the
full held amount with one usage row marked as unknown usage (`settle_orphaned`). A hold without
the marker is released (or expires after its TTL) as before. Settle and release match `held`
only, so a late answer never rewrites an orphaned hold.

A marked direct-invocation hold has two orphan handlers of its own (issue #91):
`settle_unknown_invocation`, called in process when the call fails after the provider request
with anything but an `AdapterFailure` (the usage row carries the call's attribution), and
`settle_orphaned_invocations`, the reaper's per-client sweep of such holds past their TTL (the
usage row's scope columns come from the budgets and quotas the hold names, so the settled amount
keeps counting against every rule the hold counted against).

Every statement names the hold's client next to row-level security (`tenant_id` with the hold or
run id); none relies on row-level security alone."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any  # Any: column values
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from shifde.ledger import pricing
from shifde.ledger.usage import record

RESERVATION_TTL = timedelta(minutes=10)
INVOCATION_STEP_KEY = "invoke"  # the step key of a direct invocation's hold (it has no step)

__all__ = [
    "INVOCATION_STEP_KEY",
    "RESERVATION_TTL",
    "expire_unstarted",
    "mark_provider_request",
    "release",
    "reserve_invocation",
    "reserve_step",
    "settle",
    "settle_orphaned",
    "settle_orphaned_invocations",
    "settle_unknown_invocation",
]


def reserve_step(
    session: Session,
    tenant_id: UUID,
    run_id: UUID,
    step_key: str,
    budget_ids: Sequence[UUID],
    amount: int,
    cost: Decimal,
    currency: str | None,
) -> UUID:
    """Hold `amount` tokens (and `cost`) for one run step attempt; a repeated step key re-holds the
    same row. Returns the hold's id."""
    res_id: UUID = session.execute(
        text(
            "INSERT INTO shifde.budget_reservations (id, tenant_id, run_id, step_key, "
            "budget_ids, amount, cost_amount, currency, expires_at) "
            "VALUES (:id, :t, :run, :k, :b, :a, :c, :cur, :exp) "
            "ON CONFLICT (tenant_id, run_id, step_key) WHERE run_id IS NOT NULL "
            "DO UPDATE SET status = 'held', amount = :a, cost_amount = :c, currency = :cur, "
            "budget_ids = :b, expires_at = :exp RETURNING id"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "run": run_id,
            "k": step_key,
            "b": list(budget_ids),
            "a": amount,
            "c": cost,
            "cur": currency,
            "exp": utcnow() + RESERVATION_TTL,
        },
    ).scalar_one()
    return res_id


def reserve_invocation(
    session: Session,
    tenant_id: UUID,
    budget_ids: Sequence[UUID],
    amount: int,
    cost: Decimal,
    currency: str | None,
) -> UUID:
    """Hold `amount` tokens (and `cost`) for one direct invocation (a fresh `invocation_id`).
    `budget_ids` names every budget and token quota the call was checked against: the hold has
    no run, so `budgeting._held_sql` matches it to its rules by id. Returns the hold's id."""
    res_id: UUID = session.execute(
        text(
            "INSERT INTO shifde.budget_reservations (id, tenant_id, invocation_id, step_key, "
            "budget_ids, amount, cost_amount, currency, expires_at) "
            "VALUES (:id, :t, :inv, :k, :b, :a, :c, :cur, :exp) RETURNING id"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "inv": uuid7(),
            "k": INVOCATION_STEP_KEY,
            "b": list(budget_ids),
            "a": amount,
            "c": cost,
            "cur": currency,
            "exp": utcnow() + RESERVATION_TTL,
        },
    ).scalar_one()
    return res_id


def mark_provider_request(session: Session, tenant_id: UUID, reservation_id: UUID) -> None:
    """The provider call may start now: from here on the hold is never silently freed."""
    session.execute(
        text(
            "UPDATE shifde.budget_reservations SET provider_request_at = now() "
            "WHERE id = :id AND tenant_id = :t AND status = 'held'"
        ),
        {"id": reservation_id, "t": tenant_id},
    )


def settle(
    session: Session,
    tenant_id: UUID,
    reservation_id: UUID,
    tokens: int,
    cost: Decimal | None,
) -> None:
    """Replace the hold by the recorded usage (`tokens`, `cost`)."""
    session.execute(
        text(
            "UPDATE shifde.budget_reservations SET status = 'settled', settled_amount = :a, "
            "settled_cost = :c WHERE id = :id AND tenant_id = :t AND status = 'held'"
        ),
        {"id": reservation_id, "t": tenant_id, "a": tokens, "c": cost},
    )


def release(session: Session, tenant_id: UUID, reservation_id: UUID) -> None:
    """Drop the hold (the call did not consume tokens)."""
    session.execute(
        text(
            "UPDATE shifde.budget_reservations SET status = 'released' "
            "WHERE id = :id AND tenant_id = :t AND status = 'held'"
        ),
        {"id": reservation_id, "t": tenant_id},
    )


def expire_unstarted(session: Session, tenant_id: UUID) -> None:
    """Mark `expired` the held holds of `tenant_id` past their TTL whose provider call provably
    never started (no marker); a marked hold keeps counting until it is settled or charged."""
    session.execute(
        text(
            "UPDATE shifde.budget_reservations SET status = 'expired' "
            "WHERE tenant_id = :t AND status = 'held' AND expires_at <= now() "
            "AND provider_request_at IS NULL"
        ),
        {"t": tenant_id},
    )


def settle_orphaned(session: Session, tenant_id: UUID, run_id: UUID) -> int:
    """The orphan handler of a run whose worker was lost (design 7.1): each `held` hold of run
    `run_id` in client `tenant_id` whose call may have reached the provider (marker set) becomes
    `settled_unknown` at its full amount, with one failed `model_call` usage row of the held tokens
    (`event_key = reservation_unknown:<hold id>`, `provider_reported_usage` says the usage is
    estimated and unknown); every other held hold of the run is released. A second call changes
    nothing and writes no second row. Returns the number of holds changed."""
    run = (
        session.execute(
            text(
                "SELECT id, root_run_id, workspace_id, project_id, agent_id, "
                "initiated_by_principal_id, is_test FROM shifde.runs "
                "WHERE id = :r AND tenant_id = :t"
            ),
            {"r": run_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )
    if run is None:
        return 0
    holds = session.execute(
        text(
            "SELECT res.id, res.amount, res.cost_amount, res.currency, s.id AS step_id, "
            "s.attempt, s.model_id FROM shifde.budget_reservations res "
            "LEFT JOIN LATERAL (SELECT st.id, st.attempt, st.model_id FROM shifde.run_steps st "
            "WHERE st.tenant_id = res.tenant_id AND st.run_id = res.run_id "
            "AND st.name || ':' || st.attempt = res.step_key ORDER BY st.seq DESC LIMIT 1) s "
            "ON TRUE WHERE res.tenant_id = :t AND res.run_id = :r AND res.status = 'held' "
            "AND res.provider_request_at IS NOT NULL ORDER BY res.id FOR UPDATE OF res"
        ),
        {"r": run_id, "t": tenant_id},
    ).mappings()
    changed = 0
    for h in holds.all():
        costed = (
            pricing.Costed("estimated", Decimal(h["cost_amount"]), h["currency"])
            if h["currency"] is not None
            else pricing.UNMEASURED
        )
        record(
            session,
            {
                "tenant_id": tenant_id,
                "event_key": f"reservation_unknown:{h['id']}",
                "event_type": "model_call",
                "status": "failed",
                "run_id": run["id"],
                "root_run_id": run["root_run_id"],
                "workspace_id": run["workspace_id"],
                "project_id": run["project_id"],
                "agent_id": run["agent_id"],
                "principal_id": run["initiated_by_principal_id"],
                "is_test": run["is_test"],
                "step_id": h["step_id"],
                "attempt": h["attempt"],
                "model_id": h["model_id"],
                "input_tokens": int(h["amount"]),
                "output_tokens": 0,
                "call_count": 1,
                "costed": costed,
                "provider_reported_usage": {"estimated": True, "usage_unknown": True},
            },
        )
        done = session.execute(
            text(
                "UPDATE shifde.budget_reservations SET status = 'settled_unknown', "
                "settled_amount = amount, settled_cost = cost_amount "
                "WHERE id = :id AND tenant_id = :t AND status = 'held'"
            ),
            {"id": h["id"], "t": tenant_id},
        )
        changed += int(done.rowcount or 0)  # type: ignore[attr-defined]
    released = session.execute(
        text(
            "UPDATE shifde.budget_reservations SET status = 'released' "
            "WHERE tenant_id = :t AND run_id = :r AND status = 'held' "
            "AND provider_request_at IS NULL"
        ),
        {"r": run_id, "t": tenant_id},
    )
    return changed + int(released.rowcount or 0)  # type: ignore[attr-defined]


_UNKNOWN_INVOCATION = (
    "res.tenant_id = :t AND res.status = 'held' AND res.invocation_id IS NOT NULL "
    "AND res.provider_request_at IS NOT NULL"
)
_SCOPE_COLUMN = {
    "workspace": "workspace_id",
    "project": "project_id",
    "agent": "agent_id",
    "principal": "principal_id",
}


def _charge_unknown(
    session: Session,
    tenant_id: UUID,
    hold: RowMapping,  # the hold row (id, amount, cost_amount, currency)
    attribution: Mapping[str, Any],  # Any: usage column values
) -> int:
    """One failed unknown-usage row of the held tokens, then the hold `settled_unknown` at its
    full amount (as `settle_orphaned`). Returns the number of holds changed (0 or 1)."""
    costed = (
        pricing.Costed("estimated", Decimal(hold["cost_amount"]), hold["currency"])
        if hold["currency"] is not None
        else pricing.UNMEASURED
    )
    record(
        session,
        {
            "event_type": "model_call",
            **attribution,
            "tenant_id": tenant_id,
            "event_key": f"reservation_unknown:{hold['id']}",
            "status": "failed",
            "input_tokens": int(hold["amount"]),
            "output_tokens": 0,
            "call_count": 1,
            "costed": costed,
            "provider_reported_usage": {"estimated": True, "usage_unknown": True},
        },
    )
    done = session.execute(
        text(
            "UPDATE shifde.budget_reservations SET status = 'settled_unknown', "
            "settled_amount = amount, settled_cost = cost_amount "
            "WHERE id = :id AND tenant_id = :t AND status = 'held'"
        ),
        {"id": hold["id"], "t": tenant_id},
    )
    return int(done.rowcount or 0)  # type: ignore[attr-defined]


def settle_unknown_invocation(
    session: Session,
    tenant_id: UUID,
    reservation_id: UUID,
    attribution: Mapping[str, Any],  # Any: usage column values
) -> bool:
    """A direct invocation whose call may have reached the provider failed with an unexpected
    error: its `held`, marked hold `reservation_id` in client `tenant_id` becomes
    `settled_unknown` at its full amount with one unknown-usage row
    (`event_key = reservation_unknown:<hold id>`) whose scope and model columns come from
    `attribution` (`event_type` defaults to `model_call`). A second call changes nothing and
    writes no second row. Returns whether the hold changed."""
    hold = (
        session.execute(
            text(
                # fixed fragment; values are bound
                "SELECT res.id, res.amount, res.cost_amount, res.currency "  # nosec B608
                f"FROM shifde.budget_reservations res WHERE res.id = :id AND {_UNKNOWN_INVOCATION} "
                "FOR UPDATE"
            ),
            {"id": reservation_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )
    if hold is None:
        return False
    return _charge_unknown(session, tenant_id, hold, attribution) > 0


def settle_orphaned_invocations(session: Session, tenant_id: UUID) -> int:
    """The reaper's sweep of client `tenant_id`: every `held` direct-invocation hold whose call
    may have reached the provider (marker set) and whose TTL has passed becomes `settled_unknown`
    at its full amount with one unknown-usage row (`model_call`, no model). The hold stores no
    scope, so the row's `workspace_id` / `project_id` / `agent_id` / `principal_id` are the
    `scope_id`s of the budgets and quotas its `budget_ids` name in this client (the lowest rule
    id wins a disagreement): the amount keeps counting against every rule it counted against.
    Unmarked holds and run holds are not touched. Idempotent; returns the number changed."""
    holds = session.execute(
        text(
            # fixed fragment; values are bound
            "SELECT res.id, res.amount, res.cost_amount, res.currency, "  # nosec B608
            f"res.budget_ids FROM shifde.budget_reservations res WHERE {_UNKNOWN_INVOCATION} "
            "AND res.expires_at <= now() ORDER BY res.id FOR UPDATE"
        ),
        {"t": tenant_id},
    ).mappings()
    changed = 0
    for h in holds.all():
        rules = session.execute(
            text(
                "SELECT id, scope_type, scope_id FROM shifde.budgets "
                "WHERE tenant_id = :t AND id = ANY (:b) "
                "UNION ALL SELECT id, scope_type, scope_id FROM shifde.quotas "
                "WHERE tenant_id = :t AND id = ANY (:b) ORDER BY id"
            ),
            {"t": tenant_id, "b": list(h["budget_ids"] or [])},
        ).all()
        attribution: dict[str, Any] = {}
        for _, scope_type, scope_id in rules:
            column = _SCOPE_COLUMN.get(str(scope_type))
            if column is not None:
                attribution.setdefault(column, scope_id)
        changed += _charge_unknown(session, tenant_id, h, attribution)
    return changed
