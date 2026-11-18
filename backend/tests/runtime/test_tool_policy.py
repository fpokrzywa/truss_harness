"""Findings Item 16 (REQ F-13, B-2570): a tool's own `invocation_policy` and `audit_level` in a
run's tool calls (pipeline and graph tool nodes: `test_pipeline.py`, `test_graph.py`).

Behaviour: a tool with `invocation_policy.require_approval: true` pauses every call of an agent
run for a human (`Waiting("approval")`, a pending `approval_rule` approval) and the call runs once
approved; without the flag the same call runs at once; the flag never replaces the reason of an
approval something else requires. An agent listed in `allowed_agent_ids` still calls the tool and
an unlisted agent is still refused with `agent_not_allowed`. `audit_level: "full"` adds the
scrubbed arguments and the stored output (a preview above 8 KiB) to `run.tool_invoked` when the
client captures content; `metadata`, and `full` under capture `metadata`, add neither.

Calls are driven through `steps.run_tool` with the policy an agent run builds (`agent.py`: the
run's agent artifact, `destructive` always approved), under the app role and the schema owner.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from shifde.build.env import VersionRef
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.runtime import state, steps
from tests.runtime.execution import Seed, run_context, run_row
from tests.runtime.test_capture_off import _capture
from tests.runtime.test_steps import WRITE, _approve
from tests.runtime.test_tool_audit import (
    MODES,
    READ,
    World,
    _events,
    _fake_tool,
    _new_tool,
    opened,
    seeds,
    world,
)

__all__ = ["MODES", "opened", "seeds", "world"]

SECRET = "sk-secret-value-0d9a"


def _flagged(spec: dict[str, Any], **policy: Any) -> dict[str, Any]:  # Any: spec JSON
    return {**spec, "invocation_policy": policy}


def _agent_policy(tool: VersionRef, agent: UUID, *classes: str) -> steps.ToolPolicy:
    """The policy `AgentRunner` builds for a run of `agent`."""
    return steps.ToolPolicy(
        frozenset({"destructive", *classes}), agent, frozenset({tool.version_id})
    )


def _run(
    opened: Callable[[UUID], Any],
    settings: Settings,
    x: Seed,
    tool: VersionRef,
    policy: steps.ToolPolicy,
    node: str = "t",
    args: dict[str, Any] | None = None,  # Any: tool arguments
) -> object:
    call = steps.ToolCall(tool, args or {"item": "x"}, f"node:{node}", node_id=node)
    with opened(x.tenant) as s:
        ctx = run_context(s, settings, run_row(s, x.tenant, x.running))
        return steps.run_tool(ctx, call, policy)


def _approvals(owner_db: Database, run: UUID) -> list[tuple[str, str]]:
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT reason, status FROM shifde.approvals WHERE run_id = :r "
                 "AND call_ref = 'node:t' ORDER BY epoch"), {"r": run}
        ).all()  # fmt: skip
    return [tuple(r) for r in rows]


# ------------------------------------------------------------------ require_approval
def test_a_tool_requiring_approval_pauses_an_agent_call_until_it_is_approved(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, _, _ = world
    tool = _new_tool(owner_db, a.tenant, _flagged(READ, require_approval=True))
    given = _fake_tool(monkeypatch, {"rows": [1]})
    policy = _agent_policy(tool, a.agent)
    with pytest.raises(state.Waiting) as waiting:
        _run(opened, settings, a, tool, policy)
    assert waiting.value.reason == "approval"
    assert given == []
    assert _approvals(owner_db, a.running) == [("approval_rule", "pending")]
    with pytest.raises(state.Waiting):  # still pending: asked again, not run
        _run(opened, settings, a, tool, policy)
    assert given == []
    with owner_db.session() as o:
        _approve(o, a, "approval_rule")
    assert _run(opened, settings, a, tool, policy) == {"rows": [1]}
    assert given == [{"item": "x"}]
    assert _approvals(owner_db, a.running) == [("approval_rule", "approved")]


@pytest.mark.parametrize("flag", [None, False])
def test_a_tool_without_the_flag_runs_without_an_approval(
    flag: bool | None,
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, _, _ = world
    spec = READ if flag is None else _flagged(READ, require_approval=flag)
    tool = _new_tool(owner_db, a.tenant, spec)
    given = _fake_tool(monkeypatch, {"rows": [1]})
    assert _run(opened, settings, a, tool, _agent_policy(tool, a.agent)) == {"rows": [1]}
    assert given == [{"item": "x"}]
    assert _approvals(owner_db, a.running) == []


def test_the_flag_keeps_the_reason_of_an_approval_already_required(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, _, _ = world
    tool = _new_tool(owner_db, a.tenant, _flagged(WRITE, require_approval=True))
    given = _fake_tool(monkeypatch, {"written": True})
    policy = _agent_policy(tool, a.agent, "write_non_idempotent")
    with pytest.raises(state.Waiting):
        _run(opened, settings, a, tool, policy)
    assert _approvals(owner_db, a.running) == [("tool_side_effect", "pending")]
    with owner_db.session() as o:
        _approve(o, a, "tool_side_effect")
    assert _run(opened, settings, a, tool, policy) == {"written": True}
    assert len(given) == 1


# ------------------------------------------------------------------ allowed_agent_ids
@pytest.mark.parametrize("listed", [True, False])
def test_an_agent_run_is_allowed_only_when_its_agent_is_listed(
    listed: bool,
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _, _, _ = world
    allowed = [str(a.agent if listed else a.other_agent)]
    tool = _new_tool(owner_db, a.tenant, _flagged(READ, allowed_agent_ids=allowed))
    given = _fake_tool(monkeypatch, {"rows": []})
    policy = _agent_policy(tool, a.agent)
    if listed:
        assert _run(opened, settings, a, tool, policy) == {"rows": []}
        assert len(given) == 1
        return
    with pytest.raises(state.GateDenied) as denied:
        _run(opened, settings, a, tool, policy)
    assert denied.value.reason == "agent_not_allowed"
    assert given == []


# ------------------------------------------------------------------ audit_level
def _audited(
    capture: str,
    level: str | None,
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    output: object,
) -> dict[str, Any]:  # Any: event details
    a, _, _, _ = world
    with owner_db.session() as o:
        _capture(o, a.tenant, capture)
        o.commit()
    spec = READ if level is None else {**READ, "audit_level": level}
    tool = _new_tool(owner_db, a.tenant, spec)
    _fake_tool(monkeypatch, output)
    args = {"item": "visible-arg", "api_key": SECRET, "note": f"Bearer {SECRET}"}
    _run(opened, settings, a, tool, _agent_policy(tool, a.agent), args=args)
    with owner_db.session() as o:
        [event] = _events(o, a.running)
    details: dict[str, Any] = event["details"]
    assert SECRET not in json.dumps(event, default=str)
    return details


def test_full_audit_under_capture_full_records_the_scrubbed_arguments_and_output(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = {"rows": ["visible-out"], "token": SECRET}
    details = _audited("full", "full", world, opened, owner_db, settings, monkeypatch, output)
    assert details["arguments"] == {
        "item": "visible-arg",
        "api_key": "[REDACTED]",
        "note": "[REDACTED]",
    }
    assert details["output"] == {"rows": ["visible-out"], "token": "[REDACTED]"}
    assert (details["status"], details["side_effect_class"]) == ("succeeded", "read")


def test_full_audit_caps_a_large_output_at_a_preview(
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = {"blob": "y" * 20_000}
    details = _audited("full", "full", world, opened, owner_db, settings, monkeypatch, output)
    assert details["output"]["truncated"] is True
    assert details["output"]["preview"] == json.dumps(output)[:8000]
    assert "arguments" in details


@pytest.mark.parametrize(
    ("capture", "level"),
    [("full", "metadata"), ("full", None), ("metadata", "full"), ("metadata", "metadata")],
)
def test_metadata_audit_or_capture_off_records_no_content(
    capture: str,
    level: str | None,
    world: World,
    opened: Any,
    owner_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = {"rows": ["visible-out"]}
    details = _audited(capture, level, world, opened, owner_db, settings, monkeypatch, output)
    assert not {"arguments", "args", "output"} & set(details)
    assert "visible-arg" not in json.dumps(details)
    assert "visible-out" not in json.dumps(details)
