"""SP7-4 (design 9.2 in-run and 9.3): specialist runs executed by a worker that gets the composition
root's registry (`worker_main.internal_tools()`, through `p5util.thread_worker`).

In organization A's P1, scripted specialist runs name client B's and project P2's connection,
release and target ids: every call answers `{"error": "not_found"}`, a proposed plan or evaluation
of them is stored nowhere, and the deployment specialist's `seth.board.read` gets P1's board. A
member stripped of `delivery:target:read` after the handover gets the 403 failure from
`seth.targets.read` (the run fails `authz_denied`, no target data recorded); a deactivated
requester's run fails `authz_denied`. A direct run of the installed `assist-deployment` agent,
its `assist_request_id` variable naming another request, is `not_an_assist_run` and the named
request gets no proposal. Each test has a database of its own (the full-app harness).
"""

from __future__ import annotations

from typing import Any  # Any: JSON bodies and row values
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.runtime import state
from tests._support.assist_fixtures import sent, specialist_model, tool_results
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, B
from tests._support.p4util import set_tenant_setting
from tests._support.p5util import (
    Hdr,
    limited_user,
    start_run,
    thread_worker,
    user_session,
    wait_run,
)
from tests.appgen.helpers import databricks, wire
from tests.assist.helpers import add_project, enable, installed, project_of, rows, user_of
from tests.delivery.releases import make_release
from tests.delivery.routes import address, register, target_body

NOT_FOUND = {"error": "not_found", "truncated": False}
RESEARCH, EVALUATION, DEPLOYMENT = "[[iso-k1]]", "[[iso-k2]]", "[[iso-k3]]"
TARGETS, DIRECT = "[[iso-k4]]", "[[iso-k5]]"


def _target(h: Hdr, org_slug: str, project: str) -> str:
    body = target_body("main", address(), project=project)
    return str(register(h.client, h, org_slug, body)["id"])


def _plan(target: str, release: str) -> dict[str, Any]:
    return {"target_id": target, "release_id": release, "application_key": "sales-app",
            "display_name": "Sales app"}  # fmt: skip


def _proposals(owner: Session) -> list[tuple[Any, ...]]:
    return rows(owner, "SELECT request_id::text, kind FROM shifde.assist_proposals")


def _foreign(
    client: TestClient, world: World, admin: Hdr, owner: Session, owner_db: Database
) -> dict[str, dict[str, str]]:
    """B's and P2's connection, release and target, and P1's target and release."""
    add_project(admin, "other")
    wire(admin.client)
    b = user_session(client, world.passwords, ADMIN_B)
    ws, p1 = (UUID(x) for x in project_of(owner, "demo-a"))
    p2 = UUID(project_of(owner, "demo-a", "other")[1])
    ws_b, proj_b = (UUID(x) for x in project_of(owner, "demo-b"))
    tenant_a, tenant_b = tenant_id(owner, "demo-a"), tenant_id(owner, "demo-b")
    return {
        "b": {
            "connection": databricks(b.client, b, "lake-b", B),
            "release": str(make_release(owner_db, tenant_b, ws_b, proj_b).id),
            "target": _target(b, "demo-b", "sample"),
        },
        "p2": {
            "connection": databricks(
                admin.client, admin, "lake-p2", scope={"workspace": "main", "project": "other"}
            ),  # fmt: skip
            "release": str(make_release(owner_db, tenant_a, ws, p2).id),
            "target": _target(admin, "demo-a", "other"),
        },
        "p1": {
            "release": str(make_release(owner_db, tenant_a, ws, p1).id),
            "target": _target(admin, "demo-a", "sample"),
        },
    }


def test_a_p1_run_gets_not_found_for_bs_and_p2s_ids(
    client: TestClient,
    world: World,
    owner_session: Session,
    owner_db: Database,
    settings: Settings,
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    ids = _foreign(client, world, admin, owner_session, owner_db)
    p1 = ids["p1"]
    script = {
        RESEARCH: [("seth.catalog.read", {"connection_id": ids[w]["connection"]}) for w in ("b", "p2")],
        EVALUATION: [
            *(("seth.releases.read", {"release_id": ids[w]["release"]}) for w in ("b", "p2")),
            ("seth.evaluations.read", {"release_id": ids["b"]["release"]}),
            ("seth.propose.evaluation_run", {"release_id": ids["p2"]["release"]}),
        ],
        DEPLOYMENT: [
            ("seth.board.read", {}),
            *(
                ("seth.propose.delivery_plan", _plan(p1["target"], ids[w]["release"]))
                for w in ("b", "p2")
            ),
            *(
                ("seth.propose.delivery_plan", _plan(ids[w]["target"], p1["release"]))
                for w in ("b", "p2")
            ),
        ],
    }  # fmt: skip
    enable(admin)
    installed(admin, specialist_model(admin.client, admin, script))
    set_tenant_setting(owner_session, "demo-a", "capture_content", "full")
    owner_session.commit()
    runs = {
        marker: sent(admin.client, admin, {"text": f"{words} {marker}"})
        for marker, words in (
            (RESEARCH, "research which tables hold sales"),
            (EVALUATION, "evaluate the accuracy"),
            (DEPLOYMENT, "deploy the sales app"),
        )
    }
    assert [r["specialist_key"] for r in runs.values()] == ["research", "evaluation", "deployment"]
    with thread_worker(settings):
        for out in runs.values():
            done = wait_run(admin.client, admin, out["run_id"])
            assert done["status"] == "completed", done
    for marker, out in runs.items():
        results = tool_results(owner_session, out["run_id"])
        assert [r[0] for r in results] == [k.replace(".", "-") for k, _ in script[marker]]
        for (key, _), (_, status, content) in zip(script[marker], results, strict=True):
            assert status == "succeeded", (key, content)
            if key == "seth.board.read":
                projects = content["board"]["projects"]
                assert [p["project"]["slug"] for p in projects] == ["sample"]
            else:
                assert content == NOT_FOUND, (key, content)
    assert _proposals(owner_session) == []


def _member(client: TestClient, world: World, owner: Session, admin: Hdr) -> tuple[Hdr, str]:
    perms = ["workspace:read", "project:read", "assist:request:read", "assist:request:write",
             "agent:read", "agent:execute", "prompt:read", "tool:read", "tool:invoke", "run:read", "model:read",
             "model:invoke",
             "delivery:target:read", "delivery:deployment:read", "delivery:plan:read",
             "delivery:plan:request"]  # fmt: skip
    _, role = limited_user(admin.client, owner, admin, world.passwords, "fde@demo-a.local", perms)
    return user_session(client, world.passwords, "fde@demo-a.local"), role


def test_a_member_stripped_of_target_read_mid_run_gets_the_403(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    enable(admin)
    installed(admin, specialist_model(admin.client, admin, {TARGETS: [("seth.targets.read", {})]}))
    register(admin.client, admin, "demo-a", target_body("main", address(), project="sample"))
    set_tenant_setting(owner_session, "demo-a", "capture_content", "full")
    owner_session.commit()
    member, role = _member(client, world, owner_session, admin)
    admin = user_session(client, world.passwords, ADMIN)  # creating the member signed it out
    out = sent(member.client, member, {"text": f"which release is live {TARGETS}"})
    assert out["status"] == "routed", (out["status_reason"], out)
    owner_session.execute(
        text("DELETE FROM shifde.role_permissions WHERE role_id = :r "
             "AND permission_key = 'delivery:target:read'"),
        {"r": role},
    )  # fmt: skip
    owner_session.commit()
    with thread_worker(settings):
        done = wait_run(admin.client, admin, out["run_id"])
    assert (done["status"], done["error_class"]) == ("failed", state.AUTHZ_DENIED), done
    [(name, status, content)] = tool_results(owner_session, out["run_id"])
    assert (name, status, content) == ("seth-targets-read", "failed", None)


def test_a_deactivated_requesters_run_fails_authz_denied(
    client: TestClient, world: World, owner_session: Session, settings: Settings
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    enable(admin)
    installed(admin, specialist_model(admin.client, admin, {TARGETS: [("seth.targets.read", {})]}))
    member, _ = _member(client, world, owner_session, admin)
    admin = user_session(client, world.passwords, ADMIN)  # creating the member signed it out
    out = sent(member.client, member, {"text": f"which release is live {TARGETS}"})
    assert out["status"] == "routed", (out["status_reason"], out)
    owner_session.execute(
        text("UPDATE shifde.users SET status = 'disabled' WHERE id = :u"),
        {"u": user_of(owner_session, "fde@demo-a.local")},
    )
    owner_session.commit()
    with thread_worker(settings):
        done = wait_run(admin.client, admin, out["run_id"])
    assert (done["status"], done["error_class"]) == ("failed", state.AUTHZ_DENIED), done
    assert tool_results(owner_session, out["run_id"]) == []


def test_a_direct_run_of_a_specialist_is_not_an_assist_run(
    client: TestClient,
    world: World,
    owner_session: Session,
    owner_db: Database,
    settings: Settings,
) -> None:
    admin = user_session(client, world.passwords, ADMIN)
    ws, p1 = (UUID(x) for x in project_of(owner_session, "demo-a"))
    release = str(make_release(owner_db, tenant_id(owner_session, "demo-a"), ws, p1).id)
    target = _target(admin, "demo-a", "sample")
    enable(admin)
    script = {DIRECT: [("seth.propose.delivery_plan", _plan(target, release))]}
    specialists = installed(admin, specialist_model(admin.client, admin, script))
    [deployment] = [s for s in specialists if s["key"] == "deployment"]
    set_tenant_setting(owner_session, "demo-a", "capture_content", "full")
    owner_session.commit()
    other = sent(admin.client, admin, {"text": "deploy the sales app"})  # another request
    assert other["specialist_key"] == "deployment"
    run = start_run(admin.client, admin, deployment["agent_id"], f"plan it {DIRECT}",
                    variables={"assist_request_id": other["id"], "context": ""})  # fmt: skip
    with thread_worker(settings):
        done = wait_run(admin.client, admin, run["id"])
        assert done["status"] == "completed", done
        wait_run(admin.client, admin, other["run_id"])
    [(name, status, content)] = tool_results(owner_session, run["id"])
    assert (name, status) == ("seth-propose-delivery_plan", "succeeded")
    assert content == {"error": "not_an_assist_run", "truncated": False}
    assert _proposals(owner_session) == []
    assert user_of(owner_session, ADMIN) == other["requested_by"]
