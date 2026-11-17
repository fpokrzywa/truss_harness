"""SETH 3ba0e06 `tests/test_p8_policies.py`, the four tests that need runs through the worker and
the full app: approval rules, a disabled or non-matching rule, cost thresholds and the decision
log of a run denied by a zero budget. Assertions unchanged; imports name `tests._support.*`.

Not here (moved earlier): policy validation and audit, policy administration and tenant scope,
retention settings, the data-export policy (`tests/governance/test_p8_policies.py`, B-1825);
`test_governance_side_effect_classes_match_the_build_module` (same file, B-2134);
`test_a_model_policy_writes_through_to_the_p3_allowlist_and_blocks_other_models`
(`tests/app/test_model_policies.py`).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A
from tests._support.p4util import tenant_scope
from tests._support.p5util import (
    BASE_PERMS,
    limited_user,
    start_run,
    thread_worker,
    tool_call_reply,
    user_session,
    wait_for,
    wait_run,
)
from tests._support.p8util import CURRENCY, priced_stack

ECHO = {"match": "weather", "reply": tool_call_reply("echo", {"text": "tool-output"})}
FINAL = {"match": "Tool results", "reply": "done"}


def _rows(owner: Session, sql: str, **p: Any) -> list[Any]:
    return list(owner.execute(text(sql), p).all())


def _policy(
    client: TestClient, h: dict[str, str], kind: str, definition: dict[str, Any], **extra: Any
) -> Any:
    r = client.post(
        f"{A}/governance/policies",
        json={"kind": kind, "name": f"test {kind}", "definition": definition, **extra},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _approvals(client: TestClient, h: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = client.get(f"{A}/approvals?status=pending", headers=h).json()[
        "items"
    ]
    return out


# ------------------------------------------------------------------ approval rules
def test_an_approval_rule_adds_an_approval_with_its_own_approver_permission(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session, script=[ECHO, FINAL])
    rule = _policy(
        client,
        h,
        "approval_rule",
        {"when": {"side_effect_classes": ["none"]}, "approver_permission": "governance:read"},
    )
    assert rule["enforcement"] == "enforced" and rule["definition"]["when"]
    for email, perms in (
        ("plain@demo-a.local", [*BASE_PERMS, "approval:decide"]),
        ("gov@demo-a.local", [*BASE_PERMS, "approval:decide", "governance:read"]),
    ):
        h = login(client, world.passwords, ADMIN)
        limited_user(client, owner_session, h, world.passwords, email, perms)
    h = login(client, world.passwords, ADMIN)
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        waiting = wait_run(client, h, run["id"], {"waiting_for_approval", "completed", "failed"})
        assert waiting["status"] == "waiting_for_approval", waiting
        (appr,) = _approvals(client, h)
        assert appr["reason"] == "approval_rule"
        stored = _rows(
            owner_session,
            "SELECT required_permission FROM shifde.approvals WHERE id = :i",
            i=appr["id"],
        )
        assert stored == [("governance:read",)]
        # the initiator cannot approve (separation of duties is untouched)
        assert (
            client.post(
                f"{A}/approvals/{appr['id']}/decide", json={"decision": "approve"}, headers=h
            ).status_code
            == 403
        )
        # an approver without the rule's permission is refused; one with both succeeds
        plain = user_session(client, world.passwords, "plain@demo-a.local")
        gov = user_session(client, world.passwords, "gov@demo-a.local")
        no = plain.client.post(
            f"{A}/approvals/{appr['id']}/decide", json={"decision": "approve"}, headers=plain
        )
        assert no.status_code == 403
        yes = gov.client.post(
            f"{A}/approvals/{appr['id']}/decide", json={"decision": "approve"}, headers=gov
        )
        assert yes.status_code == 200, yes.text
        done = wait_run(client, login(client, world.passwords, ADMIN), run["id"])
    assert done["status"] == "completed"
    decisions = _rows(
        owner_session,
        "SELECT action, decision, reason_codes, policy_ids FROM shifde.policy_decisions "
        "WHERE run_id = :r AND action = 'approval_required'",
        r=run["id"],
    )
    assert len(decisions) == 1 and [str(p) for p in decisions[0][3]] == [rule["id"]]
    audit = _rows(
        owner_session,
        "SELECT details FROM shifde.audit_events WHERE action = 'approval.requested'",
    )
    assert audit and str(rule["id"]) in audit[0][0]["policy_ids"]


def test_a_disabled_or_non_matching_rule_adds_no_approval_and_none_can_waive_one(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session, script=[ECHO, FINAL])
    rule = _policy(client, h, "approval_rule", {"when": {"side_effect_classes": ["none"]}})
    off = client.patch(
        f"{A}/governance/policies/{rule['id']}", json={"status": "disabled"}, headers=h
    )
    assert off.status_code == 200
    _policy(client, h, "approval_rule", {"when": {"side_effect_classes": ["destructive"]}})
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed" and done["usage"]["tool_calls"] == 1
    # there is no way to express a waiver: the definition schema only ADDS approvals
    bad = client.post(
        f"{A}/governance/policies",
        json={
            "kind": "approval_rule",
            "definition": {"when": {"side_effect_classes": ["destructive"]}, "waive": True},
        },
        headers=h,
    )
    assert bad.status_code == 422


def test_a_cost_threshold_alerts_once_per_run_and_can_require_approval_for_tool_calls(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session, script=[ECHO, FINAL])
    alert = _policy(
        client, h, "cost_threshold",
        {"threshold_amount": 0.000001, "currency": CURRENCY, "action": "alert"},
    )  # fmt: skip
    run = start_run(client, h, stack.agent["id"], "what is the weather")
    with thread_worker(settings):
        done = wait_run(client, h, run["id"])
    assert done["status"] == "completed" and done["usage"]["model_calls"] == 2
    audited = _rows(
        owner_session,
        "SELECT details FROM shifde.audit_events WHERE action = 'governance.cost_threshold'",
    )
    assert len(audited) == 1 and audited[0][0]["policy_id"] == alert["id"]  # once, not per call
    assert "unmeasured" in audited[0][0]["note"]
    logged = client.get(f"{A}/governance/decisions?run_id={run['id']}", headers=h).json()["items"]
    mine = [d for d in logged if d["action"] == "governance.cost_threshold"]
    assert (
        len(mine) == 1 and mine[0]["policy_ids"] == [alert["id"]] and mine[0]["decision"] == "allow"
    )
    # the same threshold with action require_approval stops the next tool call
    client.patch(f"{A}/governance/policies/{alert['id']}", json={"status": "disabled"}, headers=h)
    _policy(
        client, h, "cost_threshold",
        {"threshold_amount": 0.000001, "currency": CURRENCY, "action": "require_approval"},
    )  # fmt: skip
    # this run needs a tool call after a model call: the second run in the same stack
    h = login(client, world.passwords, ADMIN)
    second = start_run(client, h, stack.agent["id"], "what is the weather today", key="ct-2")
    with thread_worker(settings):
        waiting = wait_run(client, h, second["id"], {"waiting_for_approval", "completed", "failed"})
        assert waiting["status"] == "waiting_for_approval", waiting
        wait_for(lambda: _approvals(client, h))


# ------------------------------------------------------------------ administration


def test_the_decision_log_lists_denials_with_reason_codes_policy_ids_and_run(
    client: TestClient, world: World, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    stack = priced_stack(client, h, owner_session)
    from tests._support.p8util import budget

    b = budget(client, h, tenant_scope(owner_session), amount=0)
    run = start_run(client, h, stack.agent["id"], "go")
    items = client.get(
        f"{A}/governance/decisions?run_id={run['id']}&decision=deny", headers=h
    ).json()
    (d,) = items["items"]
    assert d["decision"] == "deny" and d["reason_codes"] == ["budget_denied"]
    assert d["policy_ids"] == [b["id"]] and d["run_id"] == run["id"]
    assert d["action"] == "run:start" and d["principal"] and d["resource"].startswith("run:")
    assert d["at"] == d["occurred_at"] and d["reason_code"] == "budget_denied"
    assert client.get(f"{A}/governance/decisions?decision=maybe", headers=h).status_code == 422
    overview = client.get(f"{A}/governance/overview", headers=h).json()
    assert overview["denials_7d"] >= 1 and overview["budgets"][0]["id"] == b["id"]
    assert overview["budgets"][0]["enforcement"] == "hard"
    assert set(overview) >= {"top_spenders", "pending_approvals", "policy_counts"}
