"""Ids in tool arguments resolve only in the key's client and resource scope (SP8 design 3.7 items
1 and 4, section 10 item 13): another client's run, release, deployment, project and agent
version answer exactly as an unknown id, and the `shifde_app` statement log shows no row of the
other client was ever read back."""

from __future__ import annotations

from typing import Any  # Any: tool arguments
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from tests._support.fullapp import World
from tests._support.runs import add_agent_version, add_run
from tests._support.statements import statement_log
from tests._support.tenants import add_project
from tests.delivery.releases import make_release
from tests.delivery.routes import make_deployment
from tests.mcp_access.helpers import (
    ADMIN_A,
    ORG_A,
    ORG_B,
    V1_TOOLS,
    A,
    call,
    make_key,
    ok,
    project,
    set_mcp,
    sign_in,
    tenant,
)
from tests.packaging.helpers import bindings, new_version, seal, solution, stack, version_body

NOT_FOUND = {"error": "not_found", "message": "not found"}


def _workspace(owner: Session, org: str) -> UUID:
    return UUID(
        str(
            owner.execute(
                text(
                    "SELECT w.id FROM shifde.workspaces w JOIN shifde.tenants t "
                    "ON t.id = w.tenant_id WHERE t.slug = :o AND w.slug = 'main'"
                ),
                {"o": org},
            ).scalar_one()
        )
    )


def test_another_clients_ids_answer_as_unknown_ones_and_no_row_of_it_is_read(
    mcp: TestClient, world: World, owner_session: Session, owner_db: Database, app_url: str
) -> None:
    a, b = tenant(owner_session, ORG_A), tenant(owner_session, ORG_B)
    ws_b = _workspace(owner_session, ORG_B)
    p_a, p_b = project(owner_session, ORG_A), project(owner_session, ORG_B)
    _, version_b = add_agent_version(owner_session, b)
    run_b = add_run(owner_session, b, workspace_id=ws_b, project_id=p_b)
    owner_session.commit()
    release_b = make_release(owner_db, b, ws_b, p_b).id
    deployment_b = make_deployment(owner_db, b)
    deployment_a = make_deployment(owner_db, a)
    h = sign_in(mcp, world, ADMIN_A)  # A's own sealed release, through the packaging routes
    built = stack(mcp, h)
    solution_id = solution(mcp, h, "mcp-release")["id"]
    body = version_body(built.agent_v1, dataset=built.dataset)
    version_id = new_version(mcp, h, solution_id, body)["id"]
    sealed = seal(mcp, h, solution_id, version_id, bindings({built.model: built.provider}))
    assert sealed.status_code == 201, sealed.text
    release_a = sealed.json()["id"]
    _, key = make_key(mcp, world, ADMIN_A, A)
    set_mcp(mcp, world, ADMIN_A, A, allowed_tools=sorted(V1_TOOLS))
    unknown = str(uuid7())
    run_args = {"input": "hello"}
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("get_run", {"run_id": str(run_b)}, {"run_id": unknown}),
        ("get_release", {"release_id": str(release_b)}, {"release_id": unknown}),
        ("get_deployment", {"deployment_id": str(deployment_b)}, {"deployment_id": unknown}),
        ("list_agents", {"project_id": str(p_b)}, {"project_id": unknown}),
        ("list_releases", {"project_id": str(p_b)}, {"project_id": unknown}),
        (
            "start_run",
            {"project_id": str(p_b), "agent_version_id": str(version_b), **run_args},
            {"project_id": unknown, "agent_version_id": unknown, **run_args},
        ),
        (
            "start_run",
            {"project_id": str(p_a), "agent_version_id": str(version_b), **run_args},
            {"project_id": str(p_a), "agent_version_id": unknown, **run_args},
        ),
    ]
    theirs_ids = {str(i) for i in (b, ws_b, p_b, version_b, run_b, release_b, deployment_b)}
    role = make_url(app_url).username or "shifde_app"
    with statement_log(role) as log:
        for tool, theirs, missing in cases:
            assert call(mcp, key, tool, theirs) == (True, NOT_FOUND), tool
            assert call(mcp, key, tool, missing) == (True, NOT_FOUND), tool
    assert log.statements and log.rows  # the log saw the tools' reads
    assert {v for v in log.values() if any(i in v for i in theirs_ids)} == set()
    # B's rows exist (the owner reads them back): the refusals are not about missing rows
    owner_session.rollback()
    assert (
        owner_session.execute(
            text("SELECT count(*) FROM shifde.runs WHERE id = :r AND tenant_id = :b"),
            {"r": run_b, "b": b},
        ).scalar_one()
        == 1
    )
    # the controls: A's own release and deployment, in the shapes the design allows
    release = ok(mcp, key, "get_release", {"release_id": release_a})
    assert release["id"] == release_a and "manifest" not in release
    listed = ok(mcp, key, "list_releases", {"project_id": str(p_a)})
    assert [r["id"] for r in listed["items"]] == [release_a]
    deployment = ok(mcp, key, "get_deployment", {"deployment_id": str(deployment_a)})
    assert set(deployment) == {
        "id",
        "status",
        "status_reason",
        "created_at",
        "updated_at",
        "checks",
        "journal_steps",
    }
    assert [d["id"] for d in ok(mcp, key, "list_deployments")["items"]] == [str(deployment_a)]


def test_a_project_scoped_key_gets_not_found_for_another_projects_run(
    mcp: TestClient, world: World, owner_session: Session
) -> None:
    a = tenant(owner_session, ORG_A)
    ws = _workspace(owner_session, ORG_A)
    p1 = project(owner_session, ORG_A)
    p2 = add_project(owner_session, a, ws)
    run1 = add_run(owner_session, a, workspace_id=ws, project_id=p1)
    run2 = add_run(owner_session, a, workspace_id=ws, project_id=p2)
    owner_session.commit()
    _, key = make_key(
        mcp, world, ADMIN_A, A, resource_scope={"workspace_ids": [], "project_ids": [str(p1)]}
    )
    set_mcp(mcp, world, ADMIN_A, A)
    run = ok(mcp, key, "get_run", {"run_id": str(run1)})
    assert run["run_id"] == str(run1) and run["pending_approvals"] == 0
    assert call(mcp, key, "get_run", {"run_id": str(run2)}) == (True, NOT_FOUND)
    assert call(mcp, key, "get_run", {"run_id": str(uuid7())}) == (True, NOT_FOUND)
    assert call(mcp, key, "list_agents", {"project_id": str(p2)}) == (True, NOT_FOUND)
    assert [p["id"] for p in ok(mcp, key, "list_projects")["items"]] == [str(p1)]
