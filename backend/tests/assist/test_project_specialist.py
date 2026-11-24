"""SP7-2 first check (design section 4, Call S7-B): a specialist is an ordinary project-level agent.

Projects P1 (`sample`) and P2 (`other`) of demo-a both have the `research` specialist installed
with a model on the fake provider. A user bound only as the system `developer` at P1 runs P1's
`assist-research` in P1 through the runs API and the run completes; P2's `assist-research` agent
is a 404 to them in the build API, a run of it in P1 is refused (404 or 403) and P2's
specialists list is a 404, the answer a project that does not exist gets.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p3util import ADMIN, A
from tests._support.p5util import start_run, thread_worker, user_session, wait_run
from tests.assist.helpers import (
    NO_PROJECT,
    P2,
    PROJECT,
    add_project,
    enable,
    installed,
    model,
    project_member,
    specialists,
)


def test_a_p1_developer_runs_p1s_specialist_and_not_p2s(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    p1 = admin.client.get(f"{A}{PROJECT}", headers=admin).json()["id"]
    add_project(admin, "other")
    enable(admin)
    model_id = model(admin)
    [mine] = installed(admin, model_id, project=PROJECT, keys=["research"])
    [theirs] = installed(admin, model_id, project=P2, keys=["research"])
    assert mine["status"] == theirs["status"] == "enabled"
    assert mine["agent_id"] != theirs["agent_id"]

    dev = project_member(client, world, "p1-developer@demo-a.local", p1)
    run = start_run(dev.client, dev, mine["agent_id"], "which tables do we have?")
    assert run["status"] != "failed", run
    with thread_worker(settings):
        done = wait_run(dev.client, dev, run["id"])
    assert done["status"] == "completed", done
    assert (done["agent_id"], done["agent_version_id"]) == (
        mine["agent_id"],
        mine["agent_version_id"],
    )
    assert done["project"]["id"] == mine["project_id"] == p1

    # P2's agent: invisible in the build API, and no run of it in P1
    hidden = dev.client.get(f"{A}/agents/{theirs['agent_id']}", headers=dev)
    assert hidden.status_code == 404, hidden.text
    refused = dev.client.post(
        f"{A}/runs",
        headers=dev,
        json={"kind": "agent", "agent_id": theirs["agent_id"], "workspace": "main",
              "project": "sample", "input": "hello"},
    )  # fmt: skip
    assert refused.status_code in (403, 404), refused.text

    # P2's specialists: the answer a project that does not exist gets
    r, missing = specialists(dev, project=P2), specialists(dev, project=NO_PROJECT)
    assert r.status_code == missing.status_code == 404, r.text
    assert r.json()["detail"] == missing.json()["detail"]
    own = specialists(dev)
    assert own.status_code == 200, own.text
    assert [s["agent_id"] for s in own.json()] == [mine["agent_id"]]
