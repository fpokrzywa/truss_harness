"""F-10 (#17, closes the F-10 part of B-2196): pipeline write nodes consult `approval_rule` policies.

Behaviour: a write node (api_request with a write method, connection sink) is a data export
classed by its method (DELETE `destructive`, PUT `write_idempotent`, POST / PATCH
`write_non_idempotent`) with the node's optional `target_environment`, matched ignoring case and
surrounding spaces (no label matches every environment rule; #60, B-2728); a matching rule
of the run's client opens one `approval_rule` approval (an `approval` step, an event, an audit
with the policy ids and a decision record, in one fenced transaction) BEFORE the write-ahead step,
and the run waits. Approved: the write is sent once; rejected: nothing is sent and the run fails
with APPROVAL_REJECTED. A rule only adds an approval: a granted upstream approval node does not
waive it, a GET node never consults rules, a tool call is never a data export and has no target
environment. Several matching rules give one approval that needs every rule's approver permission
(`context_summary.approver_permissions`). C1 holds when the lease is lost around the write or while the approval is pending.

Isolation, as `shifde_app` and as the schema owner: client B's identical rules (tenant-wide, or
forged to name A's client or A's workspace) never pause A's write.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import policies, state
from shifde.runtime.context import RunContext
from shifde.runtime.pipeline import PipelineRunner
from tests.runtime.test_pipeline import (
    Client,
    _api,
    _approve,
    _ctx,
    _pipe,
    _rows,
    opened,
    pair,
)
from tests.runtime.test_policies import WRITE, _policy

__all__ = ["opened", "pair"]

DOC = {"doc": {"k": "v"}}


def _rule(
    owner_db: Database,
    tenant: UUID,
    when: dict[str, Any],  # Any: JSON
    permission: str | None = None,
    scope: tuple[str, UUID] | None = None,
) -> UUID:
    definition: dict[str, Any] = {"when": when}  # Any: JSON
    if permission is not None:
        definition["approver_permission"] = permission
    with owner_db.session() as o:
        pid = _policy(o, tenant, "approval_rule", definition, scope=scope)
        o.commit()
    return pid


def _sink(nid: str, conn: UUID, **cfg: Any) -> dict[str, Any]:  # Any: node JSON
    return {"id": nid, "type": "sink",
            "config": {"target": "connection", "connection_id": str(conn), "path_template": "/x",
                       "method": "POST", **cfg},
            "inputs": {"document": "$params.doc"}}  # fmt: skip


def _write(nid: str, conn: UUID, env: str | None = None) -> dict[str, Any]:  # Any: node JSON
    node = _api(nid, conn, "POST")
    if env is not None:
        node["config"]["target_environment"] = env
    return node


def _spy(mp: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """`_invoke_connection` replaced: (node, method) of every call that would have been sent."""
    sent: list[tuple[str, str]] = []

    def invoke(
        self: PipelineRunner,
        nid: str,
        connection_id: UUID,
        request: dict[str, Any],  # Any: request JSON
        *,
        write: bool,
    ) -> dict[str, Any]:  # Any: adapter result
        sent.append((nid, str(request["method"])))
        return {"status": 200, "body": {"ok": True}}

    mp.setattr(PipelineRunner, "_invoke_connection", invoke)
    return sent


def _advance(opened: Any, settings: Settings, c: Client, run: UUID) -> Any:  # Any: run output
    with opened(c.tenant) as s:
        return PipelineRunner(_ctx(s, settings, c, run)).run()


def _waits(opened: Any, settings: Settings, c: Client, run: UUID) -> str:
    with pytest.raises(state.Waiting) as waiting:
        _advance(opened, settings, c, run)
    return waiting.value.reason


def _approvals(owner_db: Database, run: UUID) -> list[dict[str, Any]]:  # Any: rows
    return _rows(owner_db, "SELECT reason, call_ref, epoch, status, required_permission, "
                 "side_effect_class, arguments FROM shifde.approvals WHERE run_id = :r "
                 "ORDER BY id", r=run)  # fmt: skip


def _steps(owner_db: Database, run: UUID, kind: str) -> list[dict[str, Any]]:  # Any: rows
    return _rows(owner_db, "SELECT node_id, status, error_class, idempotency_key FROM "
                 "shifde.run_steps WHERE run_id = :r AND kind = :k ORDER BY seq, attempt",
                 r=run, k=kind)  # fmt: skip


def _decide(owner_db: Database, c: Client, run: UUID, status: str) -> None:
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.approvals SET status = :st, decided_by = :u, decided_at = now() "
                 "WHERE run_id = :r AND reason = 'approval_rule'"),
            {"st": status, "u": c.user, "r": run},
        )  # fmt: skip
        o.commit()


def _keyed_writes(owner_db: Database, run: UUID) -> list[dict[str, Any]]:  # Any: rows
    return [s for s in _steps(owner_db, run, "pipeline_node") if s["idempotency_key"]]


# ------------------------------------------------------------------ 1. a matching rule pauses
def test_a_matching_rule_pauses_a_write_node_before_anything_is_written_ahead_or_sent(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    pid = _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE]}, "governance:manage")
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    sent = _spy(monkeypatch)
    assert _waits(opened, settings, a, run) == "approval"
    assert sent == []
    assert _keyed_writes(owner_db, run) == []
    assert _steps(owner_db, run, "pipeline_node") == []
    [appr] = _approvals(owner_db, run)
    assert appr == {
        "reason": "approval_rule", "call_ref": "node:w", "epoch": 0, "status": "pending",
        "required_permission": "governance:manage", "side_effect_class": WRITE,
        "arguments": {"node": "w", "type": "api_request", "method": "POST",
                      "side_effect_class": WRITE, "target_environment": None},
    }  # fmt: skip
    assert _steps(owner_db, run, "approval") == [
        {"node_id": "w", "status": "running", "error_class": None, "idempotency_key": None}
    ]
    decisions = _rows(owner_db, "SELECT tenant_id, policy_ids, reason_codes FROM "
                      "shifde.policy_decisions WHERE run_id = :r AND cardinality(policy_ids) > 0",
                      r=run)  # fmt: skip
    assert decisions == [{"tenant_id": a.tenant, "policy_ids": [pid],
                          "reason_codes": ["approval_rule"]}]  # fmt: skip
    [audit] = _rows(owner_db, "SELECT details FROM shifde.audit_events WHERE tenant_id = :t "
                    "AND action = 'approval.requested'", t=a.tenant)  # fmt: skip
    assert audit["details"]["policy_ids"] == [str(pid)]
    assert audit["details"]["node"] == "w" and audit["details"]["reason"] == "approval_rule"
    # a second advance while pending waits on the same approval
    assert _waits(opened, settings, a, run) == "approval"
    assert len(_approvals(owner_db, run)) == 1 and sent == []


# ------------------------------------------------------------------ 2. approve and reject
def test_an_approved_rule_sends_the_write_once_and_closes_its_approval_step(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE]})
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    sent = _spy(monkeypatch)
    assert _waits(opened, settings, a, run) == "approval"
    _approve(owner_db, a, run, "approval_rule")
    _advance(opened, settings, a, run)
    assert sent == [("w", "POST")]
    [appr] = _approvals(owner_db, run)
    assert (appr["status"], appr["required_permission"]) == ("approved", "approval:decide")
    assert [s["status"] for s in _steps(owner_db, run, "approval")] == ["succeeded"]
    [write] = _steps(owner_db, run, "pipeline_node")
    assert write["status"] == "succeeded" and write["idempotency_key"]


def test_a_rejected_rule_fails_the_run_and_sends_nothing(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE]})
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    sent = _spy(monkeypatch)
    assert _waits(opened, settings, a, run) == "approval"
    _decide(owner_db, a, run, "rejected")
    with pytest.raises(state.RunFailed) as failed:
        _advance(opened, settings, a, run)
    assert failed.value.error_class == state.APPROVAL_REJECTED
    assert sent == []
    assert _steps(owner_db, run, "approval") == [
        {"node_id": "w", "status": "failed", "error_class": state.APPROVAL_REJECTED,
         "idempotency_key": None}
    ]  # fmt: skip
    assert _steps(owner_db, run, "pipeline_node") == [
        {"node_id": "w", "status": "failed", "error_class": state.APPROVAL_REJECTED,
         "idempotency_key": None}
    ]  # fmt: skip
    assert len(_approvals(owner_db, run)) == 1


# ------------------------------------------------------------------ 3. the new conditions
@pytest.mark.parametrize("kind", ["api_request", "sink"])
def test_a_data_export_rule_pauses_every_write_node(
    kind: str,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"data_export": True})
    node = _write("w", a.conn) if kind == "api_request" else _sink("w", a.conn)
    run = _pipe(owner_db, a, [node], params=DOC)
    sent = _spy(monkeypatch)
    assert _waits(opened, settings, a, run) == "approval"
    assert sent == []
    [appr] = _approvals(owner_db, run)
    assert appr["reason"] == "approval_rule" and appr["arguments"]["type"] == kind
    _approve(owner_db, a, run, "approval_rule")
    _advance(opened, settings, a, run)
    assert sent == [("w", "POST")]


@pytest.mark.parametrize(("env", "pauses"), [("prod", True), ("staging", False), (None, True)])
def test_a_target_environment_rule_pauses_a_write_to_a_named_or_unnamed_environment(
    env: str | None,
    pauses: bool,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write node without a label may write anywhere, so it pauses too; its approval records
    the label as written (None)."""
    a, _ = pair
    _rule(owner_db, a.tenant, {"target_environments": ["prod"]})
    run = _pipe(owner_db, a, [_write("w", a.conn, env), _sink("s", a.conn, target_environment=env)
                              if env else _sink("s", a.conn)], params=DOC)  # fmt: skip
    sent = _spy(monkeypatch)
    if pauses:
        assert _waits(opened, settings, a, run) == "approval"
        assert sent == []
        assert {r["arguments"]["target_environment"] for r in _approvals(owner_db, run)} == {env}
        assert {r["call_ref"] for r in _approvals(owner_db, run)} == {"node:w", "node:s"}
    else:
        _advance(opened, settings, a, run)
        assert sorted(sent) == [("s", "POST"), ("w", "POST")]
        assert _approvals(owner_db, run) == []


def test_an_empty_target_environment_does_not_validate(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node config schema: `target_environment` is a string of 1 to 64 characters (a run
    re-validates its pinned spec, so a published spec carrying "" is refused here too)."""
    a, _ = pair
    run = _pipe(owner_db, a, [_write("w", a.conn, "")])
    sent = _spy(monkeypatch)
    with pytest.raises(state.RunFailed) as failed:
        _advance(opened, settings, a, run)
    assert failed.value.error_class == state.VALIDATION
    assert "no longer validates" in failed.value.message
    assert sent == []


# ------------------------------------------------------------------ 3b. follow-up #60 (B-2728)
@pytest.mark.parametrize(
    ("rule_envs", "env", "pauses"),
    [
        (["prod"], "Prod", True),
        (["prod"], "PROD", True),
        (["prod"], " prod ", True),
        (["Prod"], "prod", True),
        (["prod"], None, True),
        (["prod"], "prodd", False),
        (["prod"], "staging", False),
    ],
)
def test_an_environment_rule_ignores_case_and_spaces_and_matches_an_unlabelled_write(
    rule_envs: list[str],
    env: str | None,
    pauses: bool,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1: a misspelt label is not guessed; the approval keeps the label as written."""
    a, _ = pair
    _rule(owner_db, a.tenant, {"target_environments": rule_envs})
    run = _pipe(owner_db, a, [_write("w", a.conn, env)])
    sent = _spy(monkeypatch)
    if pauses:
        assert _waits(opened, settings, a, run) == "approval"
        assert sent == []
        [appr] = _approvals(owner_db, run)
        assert appr["arguments"]["target_environment"] == env
    else:
        _advance(opened, settings, a, run)
        assert sent == [("w", "POST")]
        assert _approvals(owner_db, run) == []


def test_an_environment_rule_still_never_matches_a_tool_call_in_any_case(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"target_environments": ["prod", "Prod", " "]})
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a, run)
        for side in (WRITE, "destructive", "write_idempotent", "none"):
            assert policies.tool_approval(ctx, None, side) is None  # type: ignore[arg-type]
        assert policies.write_node_approval(ctx, None) is not None  # control: an unlabelled write


@pytest.mark.parametrize(
    ("classes", "node", "method", "pauses"),
    [
        (["destructive"], "api_request", "DELETE", True),
        (["destructive"], "api_request", "POST", False),
        (["write_idempotent"], "api_request", "PUT", True),
        (["write_idempotent"], "sink", "PUT", True),
        (["write_idempotent"], "api_request", "POST", False),
        (["write_idempotent"], "sink", "POST", False),
        ([WRITE], "api_request", "POST", True),
        ([WRITE], "api_request", "PATCH", True),
        ([WRITE], "sink", "POST", True),
        ([WRITE], "api_request", "DELETE", False),
        ([WRITE], "api_request", "PUT", False),
        ([WRITE], "sink", "PUT", False),
    ],
)
def test_a_write_node_is_classed_by_its_method(
    classes: list[str],
    node: str,
    method: str,
    pauses: bool,
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2: DELETE `destructive`, PUT `write_idempotent`, POST / PATCH `write_non_idempotent`; the
    approval row, its arguments and the `approval.requested` audit carry that class."""
    a, _ = pair
    _rule(owner_db, a.tenant, {"side_effect_classes": classes})
    n = _api("w", a.conn, method) if node == "api_request" else _sink("w", a.conn, method=method)
    run = _pipe(owner_db, a, [n], params=DOC)
    sent = _spy(monkeypatch)
    if not pauses:
        _advance(opened, settings, a, run)
        assert sent == [("w", method)]
        assert _approvals(owner_db, run) == []
        return
    assert _waits(opened, settings, a, run) == "approval"
    assert sent == []
    [appr] = _approvals(owner_db, run)
    assert appr["side_effect_class"] == classes[0]
    assert appr["arguments"]["side_effect_class"] == classes[0]
    assert appr["arguments"]["method"] == method
    [audit] = _rows(owner_db, "SELECT details FROM shifde.audit_events WHERE tenant_id = :t "
                    "AND action = 'approval.requested'", t=a.tenant)  # fmt: skip
    assert audit["details"]["side_effect_class"] == classes[0]


def _summary(owner_db: Database, run: UUID) -> list[dict[str, Any]]:  # Any: rows
    return _rows(owner_db, "SELECT call_ref, required_permission, context_summary FROM "
                 "shifde.approvals WHERE run_id = :r ORDER BY id", r=run)  # fmt: skip


@pytest.mark.parametrize(
    ("permissions", "required", "summary"),
    [
        (
            ["governance:read", "governance:manage"],
            "governance:manage",
            {"node": "w", "approver_permissions": ["governance:manage", "governance:read"]},
        ),
        (
            ["governance:read", None],
            "governance:read",
            {"node": "w", "approver_permissions": ["governance:read"]},
        ),
        (
            ["governance:read", "governance:read"],
            "governance:read",
            {"node": "w", "approver_permissions": ["governance:read"]},
        ),
        ([None, None], "approval:decide", {"node": "w"}),
    ],
)
def test_every_matched_rules_approver_permission_is_recorded(
    permissions: list[str | None],
    required: str,
    summary: dict[str, Any],  # Any: JSON
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3: one approval per node; `required_permission` is the first sorted permission and
    `context_summary.approver_permissions` all of them (sorted, distinct)."""
    a, _ = pair
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE]}, permissions[0])
    _rule(owner_db, a.tenant, {"data_export": True}, permissions[1])
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    sent = _spy(monkeypatch)
    assert _waits(opened, settings, a, run) == "approval"
    assert sent == []
    assert _summary(owner_db, run) == [
        {"call_ref": "node:w", "required_permission": required, "context_summary": summary}
    ]


# ------------------------------------------------------------------ 4. non-matches and scoping
def test_a_data_export_or_environment_rule_never_matches_a_tool_call(
    pair: tuple[Client, Client], opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"data_export": True})
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE], "target_environments": ["prod"]})
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    with opened(a.tenant) as s:
        ctx = _ctx(s, settings, a, run)
        assert policies.tool_approval(ctx, None, WRITE) is None  # type: ignore[arg-type]
        assert policies.write_node_approval(ctx, None) is not None  # control: data_export
        assert policies.write_node_approval(ctx, "prod") is not None


def test_a_get_api_request_never_consults_the_rules(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"data_export": True})
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE, "read"]})
    run = _pipe(owner_db, a, [_api("r", a.conn)])
    sent = _spy(monkeypatch)
    _advance(opened, settings, a, run)
    assert sent == [("r", "GET")]
    assert _approvals(owner_db, run) == []


def test_another_clients_rules_never_pause_this_clients_write(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B's identical rules, tenant-wide and forged to A's client id and A's workspace (the
    `_b_noise` pattern of test_policies.py), would pause A's write if a statement lost its client
    id."""
    a, b = pair
    for when in ({"data_export": True}, {"side_effect_classes": [WRITE]}):
        _rule(owner_db, b.tenant, when, "governance:manage")
        _rule(owner_db, b.tenant, when, scope=("tenant", a.tenant))
        _rule(owner_db, b.tenant, when, scope=("workspace", a.workspace))
    run = _pipe(owner_db, a, [_write("w", a.conn, "prod")])
    sent = _spy(monkeypatch)
    _advance(opened, settings, a, run)
    assert sent == [("w", "POST")]
    assert _approvals(owner_db, run) == []
    assert _rows(owner_db, "SELECT 1 FROM shifde.policy_decisions WHERE run_id = :r "
                 "AND 'approval_rule' = ANY (reason_codes)", r=run) == []  # fmt: skip


# ------------------------------------------------------------------ 5. a rule never waives
def test_a_granted_upstream_approval_node_does_not_waive_a_rules_approval(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE]})
    nodes = [{"id": "ap", "type": "approval", "config": {"message": "go"}}, _write("w", a.conn)]
    run = _pipe(owner_db, a, nodes, [{"from": "ap", "to": "w"}])
    sent = _spy(monkeypatch)
    assert _waits(opened, settings, a, run) == "approval"
    assert [r["reason"] for r in _approvals(owner_db, run)] == ["pipeline_node"]
    _approve(owner_db, a, run, "pipeline_node")
    assert _waits(opened, settings, a, run) == "approval"
    assert sent == []
    assert [(r["reason"], r["status"]) for r in _approvals(owner_db, run)] == [
        ("pipeline_node", "approved"),
        ("approval_rule", "pending"),
    ]
    _approve(owner_db, a, run, "approval_rule")
    _advance(opened, settings, a, run)
    assert sent == [("w", "POST")]


# ------------------------------------------------------------------ 6. C1 under lease loss
def test_a_lease_lost_around_an_approved_write_leaves_it_started_then_indeterminate(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE]})
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    assert _waits(opened, settings, a, run) == "approval"
    _approve(owner_db, a, run, "approval_rule")
    sent: list[str] = []

    def lost(self: PipelineRunner, nid: str, *args: Any, **kw: Any) -> dict[str, Any]:
        sent.append(nid)
        self.ctx.lease_lost.set()  # the lease is lost while the write is delivered
        raise state.LeaseLost()

    monkeypatch.setattr(PipelineRunner, "_invoke_connection", lost)
    with pytest.raises(state.LeaseLost):
        _advance(opened, settings, a, run)
    assert sent == ["w"]
    [write] = _steps(owner_db, run, "pipeline_node")
    assert write["status"] == "running" and write["idempotency_key"]  # never `failed`
    # a recovering runner with a valid lease parks the write for an operator
    assert _waits(opened, settings, a, run) == "indeterminate_side_effect"
    assert sent == ["w"]
    [write] = _steps(owner_db, run, "pipeline_node")
    assert write["status"] == "indeterminate"
    assert [(r["reason"], r["call_ref"], r["status"]) for r in _approvals(owner_db, run)] == [
        ("approval_rule", "node:w", "approved"),
        ("indeterminate_side_effect", "node:w", "pending"),
    ]


def test_a_lease_lost_while_the_rule_approval_is_requested_or_pending_writes_nothing(
    pair: tuple[Client, Client],
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = pair
    _rule(owner_db, a.tenant, {"side_effect_classes": [WRITE]})
    run = _pipe(owner_db, a, [_write("w", a.conn)])
    sent = _spy(monkeypatch)
    real = policies.write_node_approval

    def lose_first(ctx: RunContext, env: str | None, side: str) -> policies.ApprovalRule | None:
        ctx.lease_lost.set()  # lost between the rule check and the approval's transaction
        return real(ctx, env, side)

    monkeypatch.setattr(policies, "write_node_approval", lose_first)
    with pytest.raises(state.LeaseLost):
        _advance(opened, settings, a, run)
    assert _approvals(owner_db, run) == [] and _steps(owner_db, run, "approval") == []
    monkeypatch.setattr(policies, "write_node_approval", real)
    assert _waits(opened, settings, a, run) == "approval"
    # another worker takes the lease while the approval is pending
    with owner_db.session() as o:
        o.execute(text("UPDATE shifde.runs SET lease_token = :t WHERE id = :r"),
                  {"t": uuid7(), "r": run})  # fmt: skip
        o.commit()
    with pytest.raises(state.LeaseLost):
        _advance(opened, settings, a, run)
    assert _keyed_writes(owner_db, run) == [] and sent == []
    with owner_db.session() as o:  # the lease comes back to this worker's token
        o.execute(text("UPDATE shifde.runs SET lease_token = :t WHERE id = :r"),
                  {"t": a.token, "r": run})  # fmt: skip
        o.commit()
    assert _waits(opened, settings, a, run) == "approval"
    [appr] = _approvals(owner_db, run)
    assert (appr["reason"], appr["status"]) == ("approval_rule", "pending")
    assert _keyed_writes(owner_db, run) == [] and sent == []
