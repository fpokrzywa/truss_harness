"""Issue #384: the recovery of a capped, model-routed request whose re-pricing leaves no cap links
only the requester's own run of the request's client and project, also when a run of another
project or client already holds the key `assist:{id}` (`tests/assist/test_recovery_run_keys.py`
covers the other requests)."""

from __future__ import annotations

from typing import Any  # Any: row values, RunService.create's arguments

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.runtime.service import RunService
from tests._support.assist_fixtures import (
    assist_model,
    handover_hash,
    request_row,
    run_route_job,
    run_rows,
    specialist_run,
    take_key,
)
from tests._support.fullapp import World
from tests._support.p3util import B
from tests._support.p4util import set_tenant_setting
from tests._support.p5util import Hdr, user_session
from tests.assist.helpers import P2, add_project, enable, installed
from tests.mcp_access.helpers import ADMIN_B, ORG_A, ok, project
from tests.mcp_access.test_sp7_tools import (
    VAGUE_TEXT,
    _allow,
    _key,
    _requeue,
    _route_by_model,
    admin,
    model_id,
)

__all__ = ["admin", "model_id"]


def _stuck(
    mcp: TestClient, world: World, model: str, owner: Session, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], str]:
    """A capped, model-routed request of `sample` left `handing_over` with no run, and the hash of
    its run's body."""
    _route_by_model(owner, model)
    key = _key(mcp, world)
    _allow(mcp, world, max_cost_per_call=0.01)
    args = {"project_id": str(project(owner, ORG_A)), "text": VAGUE_TEXT}
    asked = ok(mcp, key, "assist_ask", args)
    bodies: list[Any] = []

    def crash(self: Any, body: Any, *args: Any, **kwargs: Any) -> Any:
        bodies.append(body)
        raise RuntimeError("the worker died")

    with monkeypatch.context() as m:
        m.setattr(RunService, "create", crash)
        with pytest.raises(RuntimeError):
            run_route_job(mcp, owner, asked["id"])
    row = request_row(owner, asked["id"])
    assert (row["status"], row["route_method"], row["run_id"]) == ("handing_over", "model", None)
    [body] = bodies
    return row, handover_hash(body)


def _recover(mcp: TestClient, owner: Session, row: dict[str, Any]) -> tuple[Any, ...]:
    """The recovery once the cap is gone (the router's model has no rate)."""
    set_tenant_setting(owner, ORG_A, "unknown_cost_policy", "deny")
    _requeue(owner, row["id"])
    assert run_route_job(mcp, owner, row["id"])
    after = request_row(owner, row["id"])
    return after["status"], after["status_reason"], after["run_id"]


@pytest.mark.parametrize(
    ("same_hash", "reason"), [(False, "idempotency_conflict"), (True, "run_project_mismatch")]
)
def test_a_capless_recovery_does_not_link_a_run_of_another_project(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    same_hash: bool,
    reason: str,
) -> None:
    p2 = add_project(admin)
    [cost] = installed(admin, model_id, project=P2, keys=["cost"])
    run = specialist_run(admin.client, admin, cost["agent_id"], "other")
    row, wanted = _stuck(mcp, world, model_id, owner_session, monkeypatch)
    take_key(owner_session, run, row["id"], **({"request_hash": wanted} if same_hash else {}))
    before = run_rows(owner_session)
    assert list(before) == [run] and str(before[run]["project_id"]) == p2
    assert before[run]["initiated_by_principal_id"] == row["requested_by"]
    assert _recover(mcp, owner_session, row) == ("failed", reason, None)
    assert run_rows(owner_session) == before


def test_a_capless_recovery_links_the_requesters_own_run_of_the_project(
    mcp: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the same built run, in the request's project."""
    [cost] = installed(admin, model_id, keys=["cost"])
    run = specialist_run(admin.client, admin, cost["agent_id"], "sample")
    row, wanted = _stuck(mcp, world, model_id, owner_session, monkeypatch)
    take_key(owner_session, run, row["id"], request_hash=wanted)
    before = run_rows(owner_session)
    status, reason, linked = _recover(mcp, owner_session, row)
    assert (status, reason, str(linked)) == ("routed", None, run)
    assert run_rows(owner_session) == before


def test_a_capless_recovery_does_not_link_a_run_of_another_client(
    mcp: TestClient,
    world: World,
    model_id: str,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = user_session(mcp, world.passwords, ADMIN_B)
    enable(b, B)
    [cost] = installed(b, assist_model(b.client, b, B), B, keys=["cost"])
    theirs = specialist_run(b.client, b, cost["agent_id"], "sample", B)
    row, wanted = _stuck(mcp, world, model_id, owner_session, monkeypatch)
    take_key(owner_session, theirs, row["id"], request_hash=wanted,
             initiated_by_principal_id=row["requested_by"])  # fmt: skip
    before = run_rows(owner_session)
    assert list(before) == [theirs] and before[theirs]["tenant_id"] != row["tenant_id"]
    assert _recover(mcp, owner_session, row) == ("failed", "budget_denied", None)
    assert run_rows(owner_session) == before
