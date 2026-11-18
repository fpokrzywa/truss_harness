"""Findings Item 15 (REQ F-06, B-2540): every tool-call attempt of a run is audited as
`run.tool_invoked`, and a run's `model.invoked` names the run, its workspace and its project.

Behaviour: a succeeded call writes one `run.tool_invoked` event with the run's client, workspace,
project and run and the tool's key and version (`success`); a non-retryable tool error writes
`failure`, an `authz_denied`-class one `denied`; a retried call writes one event per attempt; a
write whose delivery is unknown writes `failure` with `details.status` `indeterminate`. No event
ever holds an argument value, whether `capture_content` is `metadata` or `full`. A run's model call
through the real `ModelService.invoke_for_run` (a `fake`-adapter model) writes `model.invoked`
with `details.run_id`, `workspace_id` and `project_id` equal to the run's.

Isolation: after A and B each make a tool call and a model call in their own runs,
`audit.list_events` in A's `shifde_app` session lists A's `run.tool_invoked` and `model.invoked`
events (a positive control) and none of B's (by client, target or `details.run_id`), and the same
for B; A's session asking for B's client lists nothing.

Tool calls are driven under the app role and the schema owner (`opened`), model calls under the
app role (a model call's egress guard needs the client's context); the database is read as the
owner.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.audit import service as audit
from shifde.build import tool_runtime
from shifde.build.env import VersionRef
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.models_admin import schemas as ms
from shifde.runtime import state, steps
from tests.ledger.util import add_model
from tests.runtime.conftest import app_session
from tests.runtime.execution import Seed, run_context, run_row
from tests.runtime.test_capture_off import _capture
from tests.runtime.test_execution import MODES, opened, seeds
from tests.runtime.test_steps import WRITE, Extra, _tool, world

__all__ = ["MODES", "opened", "seeds", "world"]  # fixtures shared with the RT-4 / RT-6 tests

World = tuple[Seed, Seed, Extra, Extra]
READ = {**WRITE, "side_effect_class": "read", "retry": {"max_attempts": 2}}
MARKER = "arg-marker-5be1c0"


def _policy(tool: VersionRef) -> steps.ToolPolicy:
    return steps.ToolPolicy(frozenset(), None, frozenset({tool.version_id}))


def _fake_tool(monkeypatch: pytest.MonkeyPatch, *answers: object) -> list[object]:
    """`invoke_once` answers each call with the next of `answers` (raised when an exception);
    returns the list of the arguments it was given."""
    given: list[object] = []
    queue = list(answers)

    def invoke_once(conns: object, spec: object, args: object, **kw: Any) -> object:
        given.append(args)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(tool_runtime, "invoke_once", invoke_once)
    return given


def _call(
    opened: Callable[[UUID], Any],
    settings: Settings,
    x: Seed,
    tool: VersionRef,
    args: dict[str, Any] | None = None,  # Any: tool arguments
) -> object:
    """Run one call of `tool` in `x`'s running run; returns its output."""
    node = uuid7().hex
    call = steps.ToolCall(tool, args or {"item": "x"}, f"node:{node}", node_id=node)
    with opened(x.tenant) as s:
        ctx = run_context(s, settings, run_row(s, x.tenant, x.running))
        return steps.run_tool(ctx, call, _policy(tool))


def _events(o: Session, run: UUID, action: str = "run.tool_invoked") -> list[dict[str, Any]]:
    o.rollback()
    rows = o.execute(
        text(
            "SELECT * FROM shifde.audit_events WHERE action = :a AND details->>'run_id' = :r "
            "ORDER BY (details->>'step_seq')::int NULLS LAST, (details->>'attempt')::int, "
            "occurred_at"
        ),
        {"a": action, "r": str(run)},
    )
    return [dict(r) for r in rows.mappings()]


def _scope(o: Session, run: UUID) -> tuple[UUID, UUID]:
    ws, proj = o.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": run}
    ).one()
    return ws, proj


def _new_tool(owner_db: Database, tenant: UUID, spec: dict[str, Any]) -> VersionRef:
    with owner_db.session() as o:
        tool = _tool(o, tenant, spec)
        o.commit()
    return tool


# ------------------------------------------------------------------ one event per attempt
def test_a_succeeded_call_writes_one_event_with_the_runs_scope_and_the_tool(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    _fake_tool(monkeypatch, {"written": True})
    assert _call(opened, settings, a, xa.tool) == {"written": True}
    with owner_db.session() as o:
        [event] = _events(o, a.running)
        ws, proj = _scope(o, a.running)
        [(step_seq, invocation)] = o.execute(
            text("SELECT seq, tool_invocation_id FROM shifde.run_steps WHERE run_id = :r "
                 "AND kind = 'tool_call' AND idempotency_key IS NOT NULL"), {"r": a.running}
        ).all()  # fmt: skip
    assert (event["tenant_id"], event["workspace_id"], event["project_id"]) == (a.tenant, ws, proj)
    assert (event["target_type"], event["target_id"]) == ("run", str(a.running))
    assert event["outcome"] == "success"
    details = event["details"]
    assert details["run_id"] == str(a.running)
    assert details["tool_key"] == xa.tool.artifact_key
    assert details["tool_version_id"] == str(xa.tool.version_id)
    assert details["tool_version_no"] == xa.tool.version_no
    assert details["side_effect_class"] == "write_non_idempotent"
    assert (details["step_seq"], details["attempt"]) == (step_seq, 1)
    assert (details["status"], details["error_class"]) == ("succeeded", None)
    assert details["tool_invocation_id"] == str(invocation)
    assert not [k for k in details if "token" in k]


@pytest.mark.parametrize(
    ("error", "error_class", "outcome"),
    [
        (AdapterFailure(ErrorClass.CONFIG, "bad"), state.TOOL_ERROR, "failure"),
        (AdapterFailure(ErrorClass.EGRESS, "blocked"), state.AUTHZ_DENIED, "denied"),
    ],
)
def test_a_failed_attempt_writes_failure_and_an_authz_denied_one_denied(
    error: Exception,
    error_class: str,
    outcome: str,
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    _fake_tool(monkeypatch, error)
    with pytest.raises(state.RunFailed):
        _call(opened, settings, a, xa.tool)
    with owner_db.session() as o:
        [event] = _events(o, a.running)
    assert event["outcome"] == outcome
    assert (event["details"]["status"], event["details"]["error_class"]) == ("failed", error_class)


def test_a_retried_call_writes_one_event_per_attempt(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, _, _ = world
    tool = _new_tool(owner_db, a.tenant, READ)
    _fake_tool(monkeypatch, AdapterFailure(ErrorClass.TIMEOUT, "slow"), {"rows": []})
    assert _call(opened, settings, a, tool) == {"rows": []}
    with owner_db.session() as o:
        events = _events(o, a.running)
    assert [(e["details"]["attempt"], e["details"]["status"], e["outcome"]) for e in events] == [
        (1, "failed", "failure"),
        (2, "succeeded", "success"),
    ]
    assert len({e["details"]["step_seq"] for e in events}) == 1
    assert events[0]["details"]["error_class"] == state.TIMEOUT


def test_a_non_journaled_call_has_no_invocation_id(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, _, _ = world
    tool = _new_tool(owner_db, a.tenant, {**WRITE, "side_effect_class": "none"})
    _fake_tool(monkeypatch, {"ok": True})
    assert _call(opened, settings, a, tool) == {"ok": True}
    with owner_db.session() as o:
        [event] = _events(o, a.running)
    assert event["details"]["tool_invocation_id"] is None
    assert event["outcome"] == "success"


def test_an_indeterminate_write_writes_failure_with_status_indeterminate(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    _fake_tool(monkeypatch, AdapterFailure(ErrorClass.TIMEOUT, "cut"))
    with pytest.raises(state.Waiting) as waiting:
        _call(opened, settings, a, xa.tool)
    assert waiting.value.reason == "indeterminate_side_effect"
    with owner_db.session() as o:
        [event] = _events(o, a.running)
    assert event["outcome"] == "failure"
    assert (event["details"]["status"], event["details"]["error_class"]) == (
        "indeterminate",
        state.TIMEOUT,
    )


def test_a_replayed_call_writes_no_event(
    world: World, opened: Any, owner_db: Database, settings: Settings
) -> None:
    a, _, xa, _ = world
    with opened(a.tenant) as s:
        ctx = run_context(s, settings, run_row(s, a.tenant, a.running))
        assert steps.run_tool(ctx, xa.replay, _policy(xa.replay.tool)) == xa.output
    with owner_db.session() as o:
        assert _events(o, a.running) == []


# ------------------------------------------------------------------ arguments never audited
@pytest.mark.parametrize("capture", ["metadata", "full"])
def test_no_argument_reaches_the_audit_event_whatever_the_capture_setting(
    capture: str,
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, xa, _ = world
    with owner_db.session() as o:
        _capture(o, a.tenant, capture)
        o.commit()
    read = _new_tool(owner_db, a.tenant, READ)
    given = _fake_tool(
        monkeypatch,
        {"echo": MARKER},
        AdapterFailure(ErrorClass.TIMEOUT, MARKER),
        {"echo": MARKER},
        AdapterFailure(ErrorClass.CONFIG, "bad"),
    )
    assert _call(opened, settings, a, xa.tool, {"item": MARKER}) == {"echo": MARKER}
    assert _call(opened, settings, a, read, {"q": MARKER}) == {"echo": MARKER}
    with pytest.raises(state.RunFailed):
        _call(opened, settings, a, read, {MARKER: MARKER})
    assert len(given) == 4  # every call reached the tool with the marker
    with owner_db.session() as o:
        events = _events(o, a.running)
    assert len(events) == 4
    for event in events:
        row = json.dumps(event, default=str)
        assert MARKER not in row, row
        assert MARKER not in json.dumps(event["details"])
        assert not {"arguments", "args", "request_hash", "output"} & set(event["details"])


# ------------------------------------------------------------------ model.invoked
def _fake_model(o: Session, tenant: UUID) -> UUID:
    model = add_model(o, tenant, adapter_key="fake", model_key="fake-chat")
    o.execute(
        text(
            "UPDATE shifde.connections SET status = 'active' WHERE id = "
            "(SELECT p.connection_id FROM shifde.model_providers p JOIN shifde.models m "
            "ON m.provider_id = p.id WHERE m.id = :m)"
        ),
        {"m": model},
    )
    return model


def _model_call(
    s: Session, settings: Settings, x: Seed, model: UUID
) -> ms.InvokeOut:  # the real service: `invoke_for_run` is not replaced
    ctx = run_context(s, settings, run_row(s, x.tenant, x.running))
    body = ms.InvokeIn(messages=[ms.ChatMessageIn(role="user", content="hi")], max_tokens=16)
    result = ctx.artifacts.models.invoke_for_run(
        ctx.run_ref(), model, body, run_id=ctx.run_id, max_deadline=5
    )
    s.commit()
    return result.out


def test_a_runs_model_call_names_the_run_its_workspace_and_its_project(
    world: World, app_db: Database, owner_db: Database, settings: Settings
) -> None:
    """Under the app role only: a model call builds its egress guard in the client's context,
    which a schema-owner session has no part in (a worker always runs as `shifde_app`)."""
    a, _, _, _ = world
    with owner_db.session() as o:
        model = _fake_model(o, a.tenant)
        o.commit()
    with app_session(app_db, a.tenant) as s:
        _model_call(s, settings, a, model)
    with owner_db.session() as o:
        [event] = _events(o, a.running, "model.invoked")
        ws, proj = _scope(o, a.running)
    assert event["tenant_id"] == a.tenant
    assert event["outcome"] == "success"
    details = event["details"]
    assert (details["run_id"], details["workspace_id"], details["project_id"]) == (
        str(a.running),
        str(ws),
        str(proj),
    )


# ------------------------------------------------------------------ cross-client listing
def _listing(app_db: Database, session_of: UUID, tenant: UUID) -> list[audit.EventRow]:
    with app_session(app_db, session_of) as s:
        return audit.list_events(
            s,
            tenant_id=tenant,
            category=None,
            actor=None,
            action=None,
            occurred_from=None,
            occurred_to=None,
            before=None,
            limit=500,
        )


def test_a_clients_audit_listing_never_shows_the_other_clients_run_events(
    world: World,
    app_db: Database,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b, xa, xb = world
    with owner_db.session() as o:
        models = {a.tenant: _fake_model(o, a.tenant), b.tenant: _fake_model(o, b.tenant)}
        o.commit()
    _fake_tool(monkeypatch, {"written": True}, {"written": True})
    for x, extra in ((a, xa), (b, xb)):
        with app_session(app_db, x.tenant) as s:
            ctx = run_context(s, settings, run_row(s, x.tenant, x.running))
            call = steps.ToolCall(extra.tool, {"item": "x"}, "node:w", node_id="w")
            assert steps.run_tool(ctx, call, _policy(extra.tool)) == {"written": True}
            _model_call(s, settings, x, models[x.tenant])
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT id, tenant_id FROM shifde.audit_events WHERE tenant_id IN (:a, :b)"),
            {"a": a.tenant, "b": b.tenant},
        ).all()
    owners: dict[UUID, UUID] = {row[0]: row[1] for row in rows}
    for me, other in ((a, b), (b, a)):
        listed = _listing(app_db, me.tenant, me.tenant)
        mine = {
            e.action
            for e in listed
            if e.target_id == str(me.running) or e.details.get("run_id") == str(me.running)
        }
        assert {"run.tool_invoked", "model.invoked"} <= mine, mine  # positive control
        assert all(owners.get(e.id) == me.tenant for e in listed)
        assert not [e for e in listed if e.target_id == str(other.running)]
        assert not [e for e in listed if e.details.get("run_id") == str(other.running)]
        assert _listing(app_db, me.tenant, other.tenant) == []
