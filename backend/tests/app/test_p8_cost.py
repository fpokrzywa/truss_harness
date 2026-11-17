"""P8 usage accounting and cost through runs and the worker, moved from SETH 3ba0e06
`tests/test_p8_cost.py`; assertions unchanged, imports rewritten to `tests._support.*`.

Not here: `test_rate_card_versions_reject_overlaps_and_resolve_by_time` was moved to
`tests/governance/test_p8_pricing.py` (B-1805).
`test_the_migration_backfill_builds_rollups_that_reconcile_with_pre_existing_events` loads SETH's
`migrations/versions/p08_0001_governance.py` and runs its `BACKFILL` statement; the unified
`ledger_0001` creates the ledger tables empty (B-601) and has no backfill, so there is nothing of
this repository for the test to exercise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.adapters.testservers import OpenAIFakeServer
from shifde.cli.governance_seed import record_build_op
from shifde.core.settings import Settings
from shifde.ledger import pricing
from tests._support.fullapp import World
from tests._support.p2util import login, tenant_id
from tests._support.p3util import ADMIN, PLATFORM, VIEWER, A
from tests._support.p5util import oai_models, start_run, thread_worker, user_session, wait_run
from tests._support.p8util import (
    CURRENCY,
    IN_RATE,
    OUT_RATE,
    events_of,
    priced_stack,
    rate_card,
    rate_version,
)


def _expected(inp: int, out: int, ir: float = IN_RATE, orate: float = OUT_RATE) -> Decimal:
    return (Decimal(str(ir)) * inp + Decimal(str(orate)) * out) / Decimal(1_000_000)


def _run_once(
    client: TestClient, h: dict[str, str], stack: Any, settings: Settings, text_in: str
) -> dict[str, Any]:
    run = start_run(client, h, stack.agent["id"], text_in)
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed", done
    return done


# ------------------------------------------------------------------ 1-3: event, rate, attribution
def test_model_invocation_produces_a_usage_event_priced_with_a_recorded_rate(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    done = _run_once(client, h, stack, settings, "what is the weather")
    evs = [e for e in events_of(owner_session, done["id"]) if e["event_type"] == "model_call"]
    assert len(evs) == 1
    e = evs[0]
    assert e["status"] == "succeeded" and e["input_tokens"] > 0 and e["output_tokens"] > 0
    assert e["cost_kind"] == "calculated" and e["currency"] == CURRENCY
    assert Decimal(e["cost_amount"]) == _expected(e["input_tokens"], e["output_tokens"])
    snap = e["rate_snapshot"]
    assert snap["version_no"] == 1 and snap["unit"] == "per_1m_tokens"
    assert snap["rate_card_version_id"] == str(e["rate_card_version_id"])
    assert snap["source"].startswith("TEST rate card")


def test_event_is_attributed_to_the_run_project_agent_and_principal(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    done = _run_once(client, h, stack, settings, "what is the weather")
    row = owner_session.execute(
        text(
            "SELECT u.run_id, u.root_run_id, u.project_id, u.workspace_id, u.agent_id, "
            "u.principal_id, u.model_id, u.provider_key, u.model_key, u.prompt_id "
            "FROM shifde.usage_events u WHERE u.root_run_id = :r AND u.event_type = 'model_call'"
        ),
        {"r": done["id"]},
    ).one()
    project = owner_session.execute(
        text(
            "SELECT id, workspace_id FROM shifde.projects WHERE slug = 'sample' AND tenant_id = :t"
        ),
        {"t": tenant_id(owner_session, "demo-a")},
    ).one()
    assert str(row[0]) == done["id"] == str(row[1])
    assert (row[2], row[3]) == (project[0], project[1])
    assert str(row[4]) == stack.agent["id"] and str(row[6]) == stack.model
    assert (row[7], row[8]) == ("fake", "chat-a")
    assert str(row[9]) == stack.prompt["id"]  # prompt artifact id (not the version)
    # the report agrees
    summary = client.get(f"{A}/usage/summary?group_by=project", headers=h).json()
    group = next(g for g in summary["groups"] if g["key"] == str(project[0]))
    assert group["label"] == "Sample Project" or group["label"]
    e = events_of(owner_session, done["id"])[0]
    assert group["cost"]["calculated"] == pytest.approx(float(e["cost_amount"]))
    by_run = client.get(f"{A}/usage/summary?group_by=run", headers=h).json()
    assert [g["key"] for g in by_run["groups"]] == [done["id"]]


def test_changing_or_adding_rates_never_reprices_an_event_and_tenant_overrides_global(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    ph = user_session(client, world.passwords, PLATFORM)  # its own cookie jar
    stack = priced_stack(client, h, owner_session, priced=False)
    base = "/api/v1/platform"
    gcard = rate_card(ph.client, ph, base=base)
    rate_version(ph.client, ph, gcard["id"], base=base, input_rate=10.0, output_rate=20.0)
    first = _run_once(client, h, stack, settings, "what is the weather")
    e1 = events_of(owner_session, first["id"])[0]
    assert e1["rate_snapshot"]["scope"] == "global"
    assert Decimal(e1["cost_amount"]) == _expected(
        e1["input_tokens"], e1["output_tokens"], 10.0, 20.0
    )
    # the tenant adds its own card for the same model: it wins from now on
    tcard = rate_card(client, h)
    rate_version(client, h, tcard["id"], input_rate=500.0, output_rate=700.0)
    second = _run_once(client, h, stack, settings, "what is the weather today")
    e2 = events_of(owner_session, second["id"])[0]
    assert e2["rate_snapshot"]["scope"] == "tenant"
    assert Decimal(e2["cost_amount"]) == _expected(
        e2["input_tokens"], e2["output_tokens"], 500.0, 700.0
    )
    again = events_of(owner_session, first["id"])[0]
    assert (
        again["cost_amount"] == e1["cost_amount"] and again["rate_snapshot"] == e1["rate_snapshot"]
    )
    # versions are immutable, even for the database owner
    for stmt in (
        "UPDATE shifde.rate_card_versions SET input_rate = 1",
        "DELETE FROM shifde.rate_card_versions",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            owner_session.execute(text(stmt))
        owner_session.rollback()
    # and so are the events
    with pytest.raises(DBAPIError, match="append-only"):
        owner_session.execute(text("UPDATE shifde.usage_events SET cost_amount = 0"))
    owner_session.rollback()


def test_an_unpriced_model_is_unmeasured_never_zero(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session, priced=False)
    done = _run_once(client, h, stack, settings, "what is the weather")
    e = events_of(owner_session, done["id"])[0]
    assert e["cost_kind"] == "unmeasured" and e["cost_amount"] is None and e["currency"] is None
    s = client.get(f"{A}/usage/summary?group_by=model", headers=h).json()
    assert s["totals"]["cost"]["unmeasured_count"] == 1 and s["totals"]["cost"]["total_known"] == 0
    assert any("not invoices" in n for n in s["notes"])


# ------------------------------------------------------------------ 4: rollups reconcile
def test_rollups_reconcile_with_events_and_a_tampered_rollup_is_detected_and_rebuilt(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    _run_once(client, h, stack, settings, "what is the weather")
    _run_once(client, h, stack, settings, "what is the weather again")
    ledger.record(  # a second rollup row (another event type, same day)
        owner_session,
        {
            "tenant_id": tenant_id(owner_session, "demo-a"),
            "event_key": "oq-recon",
            "costed": pricing.Costed("calculated", Decimal("0"), CURRENCY),  # same kind
            "event_type": "ontology_query",
            "duration_ms": 5,
            "input_tokens": 1,
        },
    )
    owner_session.commit()
    rec = client.get(f"{A}/usage/summary?group_by=agent", headers=h).json()["reconciliation"]
    assert rec["consistent"] is True and rec["events_sum"] == rec["rollup_sum"] > 0
    assert rec["level"] == "totals"
    full = client.get(f"{A}/usage/summary?group_by=agent&reconcile=full", headers=h).json()
    assert full["reconciliation"]["level"] == "full" and full["reconciliation"]["consistent"]
    owner_session.execute(
        text(
            "UPDATE shifde.usage_rollups_daily SET input_tokens = input_tokens + 7 WHERE id = (SELECT id FROM shifde.usage_rollups_daily ORDER BY id LIMIT 1)"
        )
    )
    owner_session.commit()
    bad = client.get(f"{A}/usage/summary?group_by=agent", headers=h).json()
    assert bad["reconciliation"]["consistent"] is False
    assert bad["reconciliation"]["rollup_sum"] == bad["reconciliation"]["events_sum"] + 7
    assert bad["reconciliation"]["mismatched_rows"] >= 1
    # moving tokens between two dimension rows of the same day nets out in the control totals:
    # the default check cannot see it, the full check does
    owner_session.execute(
        text(
            "UPDATE shifde.usage_rollups_daily SET input_tokens = input_tokens - 7 WHERE id = (SELECT id FROM shifde.usage_rollups_daily ORDER BY id LIMIT 1)"
        )
    )
    owner_session.commit()
    rows = owner_session.execute(
        text("SELECT id FROM shifde.usage_rollups_daily ORDER BY id")
    ).all()
    assert len(rows) >= 2
    if True:
        owner_session.execute(
            text(
                "UPDATE shifde.usage_rollups_daily SET input_tokens = input_tokens + 11 WHERE id = :a"
            ),
            {"a": rows[0][0]},
        )
        owner_session.execute(
            text(
                "UPDATE shifde.usage_rollups_daily SET input_tokens = input_tokens - 11 WHERE id = :a"
            ),
            {"a": rows[1][0]},
        )
        owner_session.commit()
        quick = client.get(f"{A}/usage/summary?group_by=agent", headers=h).json()["reconciliation"]
        deep = client.get(f"{A}/usage/summary?group_by=agent&reconcile=full", headers=h).json()
        assert quick["consistent"] is True and deep["reconciliation"]["consistent"] is False
        owner_session.execute(
            text(
                "UPDATE shifde.usage_rollups_daily SET input_tokens = input_tokens + 7 WHERE id = (SELECT id FROM shifde.usage_rollups_daily ORDER BY id LIMIT 1)"
            )
        )
        owner_session.commit()
    # only a privileged principal may rebuild
    vh = login(client, world.passwords, VIEWER)
    assert client.post(f"{A}/usage/rollups/rebuild", headers=vh).status_code == 403
    h = login(client, world.passwords, ADMIN)
    fixed = client.post(f"{A}/usage/rollups/rebuild", headers=h)
    assert fixed.status_code == 200 and fixed.json()["consistent"] is True
    assert (
        client.get(f"{A}/usage/summary?group_by=agent", headers=h).json()["reconciliation"][
            "consistent"
        ]
        is True
    )
    # drift in the other direction: an event without a rollup (e.g. a crash between statements)
    owner_session.execute(text("DELETE FROM shifde.usage_rollups_daily"))
    owner_session.commit()
    assert (
        client.get(f"{A}/usage/summary?group_by=agent", headers=h).json()["reconciliation"][
            "consistent"
        ]
        is False
    )
    assert client.post(f"{A}/usage/rollups/rebuild", headers=h).json()["consistent"] is True


# ------------------------------------------------------------------ 5: retries counted once
def test_retries_are_separate_attempts_and_a_replayed_attempt_counts_once(
    client: TestClient,
    world: World,
    owner_session: Session,
    settings: Settings,
    oai_server: OpenAIFakeServer,
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session, priced=False)
    models = oai_models(client, h, oai_server, ["error-model", "fake-gpt"])
    card = rate_card(client, h, "fake-gpt", "openai_compatible")
    rate_version(client, h, card["id"])
    agent = stack.new_agent(
        "retry-agent",
        model={"model_id": models["error-model"], "fallback_model_ids": [models["fake-gpt"]]},
    )
    run = start_run(client, h, agent["id"], "ping")
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed", done
    evs = [e for e in events_of(owner_session, run["id"]) if e["event_type"] == "model_call"]
    assert [(e["attempt"], e["status"]) for e in evs] == [
        (1, "failed"),
        (2, "failed"),
        (3, "failed"),
        (4, "succeeded"),
    ]
    assert len({e["event_key"] for e in evs}) == 4  # one event per attempt, never merged
    failed = evs[:3]
    assert all(e["input_tokens"] == 0 and e["cost_kind"] == "unmeasured" for e in failed)
    assert evs[3]["cost_kind"] == "calculated" and evs[3]["input_tokens"] > 0
    # the same attempt written again is a no-op: no second event, no rollup change
    tid = tenant_id(owner_session, "demo-a")
    sums = "SELECT sum(calls), sum(event_count), sum(input_tokens) FROM shifde.usage_rollups_daily"
    before = owner_session.execute(text(sums)).one()
    again = ledger.record(
        owner_session,
        {
            "tenant_id": tid,
            "event_key": evs[3]["event_key"],
            "event_type": "model_call",
            "input_tokens": 999,
            "output_tokens": 0,  # setup only: B-602 refuses a succeeded call without it
            "call_count": 1,
        },
    )
    owner_session.commit()
    assert again.inserted is False
    assert owner_session.execute(text(sums)).one() == before
    summary = client.get(f"{A}/usage/summary?group_by=run", headers=h).json()
    group = summary["groups"][0]
    assert group["calls"] == 4 and group["input_tokens"] == evs[3]["input_tokens"]
    assert summary["reconciliation"]["consistent"] is True
    # failed attempts are not priced: the unmeasured count is visible, the known total is not inflated
    assert group["cost"]["unmeasured_count"] == 3


def test_a_correction_is_a_compensating_row_that_nets_out_in_the_rollups(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    done = _run_once(client, h, stack, settings, "what is the weather")
    e = events_of(owner_session, done["id"])[0]
    tid = tenant_id(owner_session, "demo-a")
    fix = ledger.correct(owner_session, tid, e["id"], reason="test correction")
    owner_session.commit()
    assert fix is not None and fix.inserted
    assert ledger.correct(owner_session, tid, e["id"], reason="again") is not None
    owner_session.commit()
    rows = owner_session.execute(
        text("SELECT count(*) FROM shifde.usage_events WHERE corrects_event_id = :e"),
        {"e": e["id"]},
    ).scalar_one()
    assert rows == 1  # the second correction was a no-op
    s = client.get(f"{A}/usage/summary?group_by=model", headers=h).json()
    assert s["totals"]["input_tokens"] == 0 and s["totals"]["cost"]["calculated"] == pytest.approx(
        0
    )
    assert s["reconciliation"]["consistent"] is True
    listed = client.get(f"{A}/usage/events?run_id={done['id']}", headers=h).json()["items"]
    assert any(i["corrects_event_id"] == str(e["id"]) for i in listed)


# ------------------------------------------------------------------ 8: estimates vs charges
def test_reports_keep_provider_charges_calculated_costs_and_estimates_apart(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    done = _run_once(client, h, stack, settings, "what is the weather")
    tid = tenant_id(owner_session, "demo-a")
    ws, pj = owner_session.execute(
        text(
            "SELECT workspace_id, id FROM shifde.projects WHERE slug = 'sample' AND tenant_id = :t"
        ),
        {"t": tid},
    ).one()
    ledger.record(  # a provider-reported charge for the same run
        owner_session,
        {
            "tenant_id": tid,
            "event_key": "pr-1",
            "event_type": "model_call",
            "workspace_id": ws,
            "project_id": pj,
            "run_id": done["id"],
            "root_run_id": done["id"],
            "input_tokens": 10,
            "output_tokens": 10,
            "costed": pricing.Costed("provider_reported", Decimal("0.5"), CURRENCY),
        },
    )
    record_build_op(  # an estimated infrastructure amount (CI hook)
        owner_session,
        org="demo-a",
        workspace="main",
        project="sample",
        build_ref="ci-42",
        duration_ms=1500,
        key=None,
        cost=Decimal("0.25"),
        currency=CURRENCY,
    )
    assert (
        record_build_op(
            owner_session,
            org="demo-a",
            workspace="main",
            project="sample",
            build_ref="ci-42",
            duration_ms=1500,
            key=None,
            cost=Decimal("0.25"),
            currency=CURRENCY,
        )
        is False
    )  # idempotent
    s = client.get(f"{A}/usage/summary?group_by=project", headers=h).json()
    cost = s["totals"]["cost"]
    e = events_of(owner_session, done["id"])[0]
    assert cost["provider_reported"] == pytest.approx(0.5)
    assert cost["calculated"] == pytest.approx(float(e["cost_amount"]))
    assert cost["estimated_infra"] == pytest.approx(0.25)
    assert cost["total_known"] == pytest.approx(0.5 + 0.25 + float(e["cost_amount"]))
    assert s["reconciliation"]["consistent"] is True
    only = client.get(
        f"{A}/usage/summary?group_by=project&cost_kind=estimated_infra", headers=h
    ).json()
    assert only["totals"]["cost"]["estimated_infra"] == pytest.approx(0.25)
    assert only["totals"]["cost"]["provider_reported"] == 0
    builds = client.get(f"{A}/usage/summary?group_by=build", headers=h).json()
    assert [g["key"] for g in builds["groups"]] == ["ci-42"]
    detail = client.get(f"{A}/usage/runs/{done['id']}", headers=h).json()
    fin = detail["final_reconciled_cost"]
    assert fin["basis"] == "mixed" and "includes_calculated_not_invoice" in fin["flags"]
    assert fin["amount"] == pytest.approx(0.5 + float(e["cost_amount"]))
    kinds = {ev["cost_kind"] for ev in detail["events"]}
    assert kinds == {"calculated", "provider_reported"}
    assert all(
        ev["rate_snapshot"] is None or "version_no" in ev["rate_snapshot"]
        for ev in detail["events"]
    )
    # the CSV export carries the kind of every row
    csv_text = client.get(f"{A}/usage/export?format=csv", headers=h)
    assert csv_text.status_code == 200 and "cost_kind" in csv_text.text.splitlines()[0]
    assert "estimated_infra" in csv_text.text and "provider_reported" in csv_text.text


def test_other_event_types_are_recorded_and_unmeasured_is_not_counted_for_them(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    tid = tenant_id(owner_session, "demo-a")
    ledger.record(
        owner_session,
        {"tenant_id": tid, "event_key": "oq-1", "event_type": "ontology_query", "duration_ms": 12},
    )
    owner_session.commit()
    s = client.get(f"{A}/usage/summary?group_by=model", headers=h).json()
    assert s["totals"]["calls"] == 0 or s["groups"] == []  # no model dimension on that event
    ev = client.get(f"{A}/usage/events?event_type=ontology_query", headers=h).json()["items"]
    assert len(ev) == 1 and ev[0]["cost_kind"] == "unmeasured"
    assert ev[0]["cost_amount"] is None
    now = datetime.now(UTC)
    assert ev[0]["occurred_at"][:10] == now.date().isoformat()
