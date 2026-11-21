"""Runtime part 5 (RT-5): `runtime/policies.py`, the governance approval rules and cost thresholds
evaluated inside a run (FR-GOV-004/005).

Behaviour: a matching approval rule adds an approval with its own approver permission; a disabled
or non-matching rule adds none; a cost rule counts the known cost of the run tree; a cost
threshold alerts once per run and policy and can require approval for tool calls.

Isolation: only the run's own client's policies, usage events and decisions count. Client B's
rules (tenant-wide, or forged to name A's client or A's workspace), B's usage events naming A's
root run and B's decision rows naming A's run and policy are all present; each test runs as
`shifde_app` in A's context and as the schema owner (no row-level security), where only the
client id in each statement keeps B's rows out.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ledger import pricing
from shifde.ledger.usage import EventResult
from shifde.runtime import policies
from shifde.runtime.context import RunContext
from tests._support.tenants import add_tenants
from tests.runtime.conftest import app_session, insert
from tests.runtime.execution import Seed, run_context, run_row, seed_client

MODES = ["app-role", "schema-owner"]
WRITE = "write_non_idempotent"


@pytest.fixture
def seeds(owner_db: Database) -> tuple[Seed, Seed]:
    a, b = uuid7(), uuid7()
    with owner_db.session() as o:
        add_tenants(o, a, b)
        pair = seed_client(o, a), seed_client(o, b)
        o.commit()
    return pair


@pytest.fixture(params=MODES)
def opened(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database
) -> Callable[[UUID], Any]:
    @contextmanager
    def session(tenant: UUID) -> Iterator[Session]:
        if request.param == "app-role":
            with app_session(app_db, tenant) as s:
                yield s
        else:
            with owner_db.session() as s:
                set_tenant(s, tenant)  # the worker's context; the owner ignores row-level security
                yield s

    return session


def _policy(
    o: Session,
    tenant: UUID,
    kind: str,
    definition: dict[str, Any],  # Any: JSON
    *,
    scope: tuple[str, UUID] | None = None,
    status: str = "active",
) -> UUID:
    pid = uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.governance_policies (id, tenant_id, kind, scope_type, scope_id, "
            "definition, status) VALUES (:i, :t, :k, :st, :si, CAST(:d AS jsonb), :s)"
        ),
        {"i": pid, "t": tenant, "k": kind, "st": scope[0] if scope else None,
         "si": scope[1] if scope else None, "d": json.dumps(definition), "s": status},
    )  # fmt: skip
    return pid


def _usage(o: Session, tenant: UUID, root: UUID, amount: str) -> None:
    insert(o, "usage_events", id=uuid7(), tenant_id=tenant, event_key=f"k-{uuid7()}",
           event_type="model_call", run_id=root, root_run_id=root, cost_kind="calculated",
           cost_amount=Decimal(amount), currency="USD")  # fmt: skip


def _b_noise(o: Session, a: Seed, b: Seed, workspace: UUID) -> list[UUID]:
    """B's rules that would apply to A's run if a statement lost its client id: tenant-wide, forged
    to A's client id and to A's workspace; B's cost threshold at 0; B's cost on A's root run."""
    rule = {"when": {"side_effect_classes": [WRITE]}, "approver_permission": "b:decide"}
    ids = [
        _policy(o, b.tenant, "approval_rule", rule),
        _policy(o, b.tenant, "approval_rule", rule, scope=("tenant", a.tenant)),
        _policy(o, b.tenant, "approval_rule", rule, scope=("workspace", workspace)),
        _policy(o, b.tenant, "cost_threshold",
                {"threshold_amount": "0", "currency": "USD", "action": "require_approval"}),
    ]  # fmt: skip
    _usage(o, b.tenant, a.running, "100")
    return ids


def _ctx(s: Session, settings: Settings, seed: Seed) -> RunContext:
    return run_context(s, settings, run_row(s, seed.tenant, seed.running))


def test_an_approval_rule_of_the_runs_client_adds_an_approval_and_bs_rules_never_do(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings, owner_db: Database
) -> None:
    a, b = seeds
    with owner_db.session() as o:
        ws = run_row(o, a.tenant, a.running)["workspace_id"]
        b_ids = _b_noise(o, a, b, ws)
        rule = {"when": {"side_effect_classes": [WRITE]}, "approver_permission": "a:decide"}
        mine = _policy(o, a.tenant, "approval_rule", rule)
        _policy(o, a.tenant, "approval_rule", rule, status="disabled")
        _policy(o, a.tenant, "approval_rule", {"when": {"side_effect_classes": ["destructive"]}})
        _policy(o, a.tenant, "approval_rule", {"when": {}})  # refused by validation; ignored
        o.commit()
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a)
        found = policies.tool_approval(ctx, None, WRITE)
        assert found == policies.ApprovalRule((mine,), ("a:decide",))
        assert policies.tool_approval(ctx, None, "read") is None
        assert not set(b_ids) & {p["id"] for p in policies._policies(ctx, "approval_rule")}
        assert not set(b_ids) & {p["id"] for p in policies._policies(ctx, "cost_threshold")}


def test_cost_rules_count_only_the_runs_own_clients_cost(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings, owner_db: Database
) -> None:
    a, b = seeds
    with owner_db.session() as o:
        ws = run_row(o, a.tenant, a.running)["workspace_id"]
        _b_noise(o, a, b, ws)
        _usage(o, a.tenant, a.running, "3")
        over_50 = _policy(o, a.tenant, "approval_rule",
                          {"when": {"estimated_cost_gt": "50", "currency": "USD"}})  # fmt: skip
        over_2 = _policy(o, a.tenant, "approval_rule",
                         {"when": {"estimated_cost_gt": "2", "currency": "USD"}})  # fmt: skip
        threshold = _policy(o, a.tenant, "cost_threshold",
                            {"threshold_amount": "2.5", "currency": "USD",
                             "action": "require_approval"})  # fmt: skip
        o.commit()
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a)
        assert policies.run_cost(s, ctx, "USD") == Decimal("3")
        assert policies.run_cost(s, ctx, "EUR") == Decimal("0")
        found = policies.tool_approval(ctx, None, WRITE)
        assert found is not None and set(found.policy_ids) == {over_2, threshold}
        assert over_50 not in found.policy_ids


def _alerts(o: Session, tenant: UUID, run: UUID) -> list[Any]:  # Any: rows
    o.rollback()
    return list(
        o.execute(
            text(
                "SELECT policy_ids FROM shifde.policy_decisions WHERE tenant_id = :t "
                "AND run_id = :r AND 'cost_threshold' = ANY (reason_codes)"
            ),
            {"t": tenant, "r": run},
        ).scalars()
    )


def test_a_cost_threshold_alerts_once_per_run_and_only_for_the_runs_own_client(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings, owner_db: Database
) -> None:
    a, b = seeds
    with owner_db.session() as o:
        ws = run_row(o, a.tenant, a.running)["workspace_id"]
        b_ids = _b_noise(o, a, b, ws)
        _usage(o, a.tenant, a.running, "3")
        mine = _policy(o, a.tenant, "cost_threshold",
                       {"threshold_amount": "2", "currency": "USD", "action": "alert"})  # fmt: skip
        high = _policy(o, a.tenant, "cost_threshold",
                       {"threshold_amount": "50", "currency": "USD", "action": "alert"})  # fmt: skip
        # B's decision row naming A's run and A's policy must not count as A's alert
        insert(o, "policy_decisions", id=uuid7(), tenant_id=b.tenant, action="x",
               decision="allow", reason_codes=["cost_threshold"], run_id=a.running,
               policy_ids=[mine])  # fmt: skip
        o.commit()
        b_before = (_alerts(o, b.tenant, a.running), _alerts(o, b.tenant, b.running))
    event = EventResult(True, uuid7(), pricing.Costed("calculated", Decimal("3"), "USD"))
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a)
        policies.after_model_call(ctx, event)
        policies.after_model_call(ctx, event)  # once per run and policy
        policies.after_model_call(ctx, EventResult(True, uuid7()))  # unmeasured: nothing
    with owner_db.session() as o:
        assert _alerts(o, a.tenant, a.running) == [[mine]]
        assert high not in _alerts(o, a.tenant, a.running)[0]
        assert not set(b_ids) & set(_alerts(o, a.tenant, a.running)[0])
        assert (_alerts(o, b.tenant, a.running), _alerts(o, b.tenant, b.running)) == b_before
        audit = o.execute(
            text(
                "SELECT tenant_id, details->>'policy_id' FROM shifde.audit_events "
                "WHERE action = 'governance.cost_threshold'"
            )
        ).all()
        assert [tuple(r) for r in audit] == [(a.tenant, str(mine))]


def test_a_rule_decision_is_written_for_the_runs_own_client_in_the_callers_transaction(
    seeds: tuple[Seed, Seed], opened: Any, settings: Settings, owner_db: Database
) -> None:
    a, b = seeds
    pid = uuid7()
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a)
        with ctx.txn():
            policies.record_rule_decision(ctx, "tool:invoke", [pid], "approval_rule")
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT tenant_id, run_id, decision, reason_codes FROM shifde.policy_decisions "
                "WHERE :p = ANY (policy_ids)"
            ),
            {"p": pid},
        ).all()
        assert [tuple(r) for r in rows] == [(a.tenant, a.running, "deny", ["approval_rule"])]
