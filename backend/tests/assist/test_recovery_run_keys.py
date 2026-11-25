"""Issue #384: the recovery of an interrupted handover finds its run by the key `assist:{id}`. A run
that already holds that key is linked only when it is the requester's, in the request's client and
project. Such runs are built as the schema owner, with the `request_hash` the recovery computes as
the worst case: a client can no longer send the key, and the body of another project never hashes
to that value."""

from __future__ import annotations

from typing import Any  # Any: row values, RunService.create's arguments

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.assist import service as assist
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
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import Hdr, user_session
from tests.assist.helpers import P2, add_project, enable, installed, project_of, rows

COST_TEXT = "how much did we spend last week"


@pytest.fixture
def admin(client: TestClient, world: World) -> Hdr:
    h = user_session(client, world.passwords, ADMIN)
    enable(h)
    return h


@pytest.fixture
def model_id(admin: Hdr) -> str:
    return assist_model(admin.client, admin)


def _stuck(
    admin: Hdr, monkeypatch: pytest.MonkeyPatch, owner: Session
) -> tuple[dict[str, Any], str]:
    """A request of `sample` left `handing_over` with no run, and the hash of its run's body."""
    bodies: list[Any] = []

    def crash(self: Any, body: Any, *args: Any, **kwargs: Any) -> Any:
        bodies.append(body)
        raise RuntimeError("the API process died")

    with monkeypatch.context() as m:
        m.setattr(assist.RunService, "create", crash)
        with pytest.raises(RuntimeError):
            admin.client.post(f"{A}/workspaces/main/projects/sample/assist/requests",
                              headers=admin, json={"text": COST_TEXT})  # fmt: skip
    [(request_id,)] = rows(owner, "SELECT id::text FROM shifde.assist_requests")
    row = request_row(owner, request_id)
    assert (row["status"], row["run_id"]) == ("handing_over", None)
    assert run_rows(owner) == {}
    [body] = bodies
    return row, handover_hash(body)


@pytest.mark.parametrize(
    ("same_hash", "reason"), [(False, "idempotency_conflict"), (True, "run_project_mismatch")]
)
def test_a_run_of_another_project_with_the_requests_key_is_not_linked(
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    same_hash: bool,
    reason: str,
) -> None:
    installed(admin, model_id, keys=["cost"])
    p2 = add_project(admin)
    [cost] = installed(admin, model_id, project=P2, keys=["cost"])
    row, wanted = _stuck(admin, monkeypatch, owner_session)
    run = specialist_run(admin.client, admin, cost["agent_id"], "other")
    take_key(owner_session, run, row["id"], **({"request_hash": wanted} if same_hash else {}))
    before = run_rows(owner_session)
    assert list(before) == [run] and str(before[run]["project_id"]) == p2
    assert before[run]["initiated_by_principal_id"] == row["requested_by"]
    assert (before[run]["request_hash"] == wanted) is same_hash
    assert run_route_job(admin.client, owner_session, row["id"])
    after = request_row(owner_session, row["id"])
    assert (after["status"], after["status_reason"], after["run_id"]) == ("failed", reason, None)
    assert run_rows(owner_session) == before


def test_the_requesters_own_run_of_the_requests_project_is_linked(
    admin: Hdr, model_id: str, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the same recovery, with the same built run but in the request's project."""
    [cost] = installed(admin, model_id, keys=["cost"])
    row, wanted = _stuck(admin, monkeypatch, owner_session)
    run = specialist_run(admin.client, admin, cost["agent_id"], "sample")
    take_key(owner_session, run, row["id"], request_hash=wanted)
    before = run_rows(owner_session)
    assert run_route_job(admin.client, owner_session, row["id"])
    after = request_row(owner_session, row["id"])
    assert (after["status"], after["status_reason"]) == ("routed", None)
    assert str(after["run_id"]) == run and run_rows(owner_session) == before


def test_a_run_of_another_client_with_the_requests_key_is_not_linked(
    client: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed(admin, model_id, keys=["cost"])
    row, wanted = _stuck(admin, monkeypatch, owner_session)
    b = user_session(client, world.passwords, ADMIN_B)
    enable(b, B)
    [cost] = installed(b, assist_model(b.client, b, B), B, keys=["cost"])
    theirs = specialist_run(b.client, b, cost["agent_id"], "sample", B)
    take_key(owner_session, theirs, row["id"], request_hash=wanted,
             initiated_by_principal_id=row["requested_by"])  # fmt: skip
    before = run_rows(owner_session)
    assert list(before) == [theirs] and before[theirs]["tenant_id"] != row["tenant_id"]
    assert run_route_job(client, owner_session, row["id"])
    after = request_row(owner_session, row["id"])
    assert (after["status"], after["status_reason"]) == ("routed", None)
    runs = run_rows(owner_session)
    assert runs.pop(theirs) == before[theirs]
    [(made, own)] = runs.items()  # a run of its own
    assert str(after["run_id"]) == made != theirs
    assert own["tenant_id"] == row["tenant_id"] and own["project_id"] == row["project_id"]
    assert str(own["project_id"]) == project_of(owner_session, "demo-a")[1]
    assert own["idempotency_key"] == f"assist:{row['id']}" and own["request_hash"] == wanted
