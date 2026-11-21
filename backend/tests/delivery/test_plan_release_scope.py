"""A plan's release fields at read time (issue #99): a plan read by a caller who cannot read its
release (`packaging:solution:read` on the release's solution scope) stays visible, with
`release_id`, `release_digest` and `evaluation_run_id` null; a release reader gets them as
stored. The request-time 404 is `test_release_gate.py`.
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies
from uuid import UUID

from shifde.core.ids import uuid7
from tests.connections.routes import as_user, grant
from tests.delivery.releases import make_release
from tests.delivery.routes import Flow, plan_row, plans
from tests.identity import harness
from tests.tenancy.conftest import World, owner_member

RELEASE_FIELDS = ("release_id", "release_digest", "evaluation_run_id")


def _plan_reader(flow: Flow, workspace_id: UUID) -> str:
    """A member whose one custom role holds only `delivery:plan:read`, bound at the workspace."""
    email = f"plan-reader-{uuid7().hex[-12:]}@route.test"
    with flow.owner_db.session() as o:
        uid = harness.person(o, email)
        owner_member(o, flow.tenant, uid)
        o.commit()
    grant(
        flow.owner_db,
        flow.tenant,
        uid,
        ["delivery:plan:read"],
        scope_type="workspace",
        scope_id=workspace_id,
    )
    return email


def _read(flow: Flow, email: str, plan: dict[str, Any]) -> tuple[Any, Any]:
    """(get body, the plan's list item) as `email`."""
    h = as_user(flow.client, email)
    got = flow.client.get(f"{plans(flow.slug)}/{plan['id']}", headers=h)
    listed = flow.client.get(plans(flow.slug), params={"target": plan["target_id"]}, headers=h)
    flow.client.cookies.clear()
    assert got.status_code == 200, got.text
    assert listed.status_code == 200, listed.text
    items = [p for p in listed.json()["items"] if p["id"] == plan["id"]]
    assert len(items) == 1
    return got.json(), items[0]


def _workspace_wide_plan(flow: Flow, world: World) -> dict[str, Any]:
    """A sealed plan on a workspace-wide target naming a release of a project of its workspace."""
    target = flow.target()
    assert target["project"] is None
    rel = make_release(flow.owner_db, world.a.tenant, world.a.ws, world.a.project)
    return flow.sealed(target["id"], release_id=rel.id)


def test_a_plan_reader_without_release_read_gets_the_plan_without_its_release(
    flow: Flow, world: World
) -> None:
    plan = _workspace_wide_plan(flow, world)
    reader = _plan_reader(flow, world.a.ws)
    stored = plan_row(flow.owner_db, plan["id"])
    for body in _read(flow, reader, plan):
        assert all(body[f] is None for f in RELEASE_FIELDS), body
        assert body["id"] == plan["id"]
        assert body["status"] == "sealed"
        assert body["digest"] == stored["digest"] is not None


def test_a_release_reader_gets_the_plans_release_fields_as_stored(flow: Flow, world: World) -> None:
    plan = _workspace_wide_plan(flow, world)
    stored = plan_row(flow.owner_db, plan["id"])
    expected = {
        "release_id": str(stored["release_id"]),
        "release_digest": stored["release_digest"],
        "evaluation_run_id": str(stored["evaluation_run_id"]),
    }
    assert all(v not in (None, "None") for v in expected.values())
    for body in _read(flow, flow.requester, plan):
        assert {f: body[f] for f in RELEASE_FIELDS} == expected
