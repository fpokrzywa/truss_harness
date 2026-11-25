"""A stored target whose bindings no longer validate (#406), on every platform: the row is listed,
read and archived as `{"unusable": true}` and its neighbours as ever; a plan request, an approval and
the job context answer 409 `target_bindings_invalid`; nothing echoes or logs a binding value; and
another client, or another project's member, gets the answer an unknown id gets.

The rows are changed as the schema owner: registration refuses such bindings, so they stand for a
row an earlier version wrote (a Fabric one before a binding was required, a Palantir one whose
`frontend_origin` is not public)."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any  # Any: JSON bodies, bindings, rows
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery import service
from tests.board.conftest import add_project
from tests.connections.routes import as_user
from tests.delivery.conftest import make_executor
from tests.delivery.docker import docker_release, docker_target, workspace_target
from tests.delivery.palantir import palantir_release, palantir_target, project_administrator
from tests.delivery.routes import (
    SNOWFLAKE_BINDINGS,
    SP8_BINDINGS,
    Flow,
    address,
    bearer,
    enqueue,
    job_url,
    lease,
    owner_sql,
    plan_row,
    plans,
    proof,
    register,
    request_plan,
    snowflake_address,
    snowflake_body,
    sp8_address,
    sp8_body,
    target_body,
    targets,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, ws_slug

MARK = "unreadable marker"
VALID: dict[str, dict[str, Any]] = {
    "databricks": {"warehouse_id": "abc123def456"},
    "snowflake": SNOWFLAKE_BINDINGS,
    **SP8_BINDINGS,
}
# per platform: a binding an earlier version did not require, and one it took with another value
MISSING = {
    "databricks": "warehouse_id",
    "snowflake": "cortex_model",
    "fabric": "lakehouse_name",
    "palantir": "frontend_asset_sha256",
    "docker": "port",
}
CHANGED: dict[str, dict[str, Any]] = {
    "databricks": {"warehouse_id": MARK},
    "snowflake": {"package": MARK},
    "fabric": {"lakehouse_name": MARK},
    "palantir": {"frontend_origin": "https://unreadable-marker.internal"},
    "docker": {"port": MARK},
}
UNUSABLE = {"unusable": True}
HOW = ("missing", "changed", "not-an-object")


def _stored(platform: str, how: str) -> Any:  # Any: JSON bindings
    if how == "missing":
        return {k: v for k, v in VALID[platform].items() if k != MISSING[platform]}
    if how == "changed":
        return {**VALID[platform], **CHANGED[platform]}
    return [MARK]


def _body(platform: str, workspace: str, **overrides: Any) -> dict[str, Any]:
    if platform == "databricks":
        return target_body(workspace, address(), **overrides)
    if platform == "snowflake":
        return snowflake_body(workspace, snowflake_address(), **overrides)
    return sp8_body(platform, workspace, sp8_address(platform), **overrides)


def _break(owner_db: Database, target_id: object, bindings: Any) -> None:  # Any: JSON bindings
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_targets SET bindings = CAST(:b AS jsonb) WHERE id = :g",
        b=json.dumps(bindings),
        g=target_id,
    )


def _broken(flow: Flow, platform: str, how: str, **overrides: Any) -> dict[str, Any]:
    """A registered target of `platform` whose stored bindings are then changed."""
    h = as_user(flow.client, flow.requester)
    made: dict[str, Any] = register(
        flow.client, h, flow.slug, _body(platform, flow.workspace, **overrides)
    )
    flow.client.cookies.clear()
    _break(flow.owner_db, made["id"], _stored(platform, how))
    return made


def _rows(owner_db: Database, tenant: UUID) -> list[list[tuple[Any, ...]]]:
    """The organization's delivery rows, read as the schema owner."""
    with owner_db.session() as o:
        return [
            [
                tuple(r)
                for r in o.execute(
                    # the table names are the literals below
                    text(f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY id"),  # nosec B608
                    {"t": tenant},
                )
            ]
            for table in (
                "delivery_targets",
                "delivery_artifacts",
                "delivery_plans",
                "delivery_jobs",
                "delivery_deployments",
            )
        ]


def _refused(body: dict[str, Any]) -> None:
    assert body["code"] == "target_bindings_invalid"
    assert "marker" not in json.dumps(body)


# ------------------------------------------------------------------ reads and archive
@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("platform", VALID)
def test_an_unreadable_target_is_listed_read_and_archived_as_unusable(
    flow: Flow, caplog: pytest.LogCaptureFixture, platform: str, how: str
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = _broken(flow, platform, how)
    h = as_user(flow.client, flow.requester)
    good = register(flow.client, h, flow.slug, _body(platform, flow.workspace))
    r = flow.client.get(f"{targets(flow.slug)}?limit=200", headers=h)
    assert r.status_code == 200, r.text
    listed = {t["id"]: t for t in r.json()["items"]}
    shown = {**bad, "bindings": UNUSABLE, "updated_at": listed[bad["id"]]["updated_at"]}
    assert listed == {good["id"]: good, bad["id"]: shown}
    r = flow.client.get(f"{targets(flow.slug)}/{bad['id']}", headers=h)
    assert r.status_code == 200 and r.json() == listed[bad["id"]], r.text
    r = flow.client.get(f"{targets(flow.slug)}/{good['id']}", headers=h)
    assert r.status_code == 200 and r.json() == good, r.text
    warned = [m.getMessage() for m in caplog.records if m.name == "shifde.delivery.service"]
    assert set(warned) == {f"delivery target {bad['id']}: its stored bindings no longer validate"}
    r = flow.client.post(f"{targets(flow.slug)}/{bad['id']}/archive", headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["bindings"]) == ("archived", UNUSABLE)
    assert "marker" not in caplog.text and "marker" not in r.text
    # its claim is free again
    again = _body(platform, flow.workspace, address=bad["address"])
    assert register(flow.client, h, flow.slug, again)["bindings"] == good["bindings"]


@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("platform", ["snowflake", "docker"])
def test_another_target_is_registered_on_an_unreadable_targets_claim(
    flow: Flow, platform: str, how: str
) -> None:
    """The `binding_in_use` check reads the stored row of the claim."""
    bad = _broken(flow, platform, how)
    other = {
        "snowflake": {**SNOWFLAKE_BINDINGS, "package": "P2", "application": "A2"},
        "docker": {**VALID["docker"], "project": "another", "port": 9000},
    }[platform]
    body = _body(platform, flow.workspace, address=bad["address"], bindings=other)
    register(flow.client, as_user(flow.client, flow.requester), flow.slug, body)


# ------------------------------------------------------------------ plan request and job context
@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("platform", VALID)
def test_a_plan_request_on_an_unreadable_target_is_refused_without_a_write(
    flow: Flow, platform: str, how: str
) -> None:
    bad = _broken(flow, platform, how)
    release = flow.release(bad["id"])
    before = _rows(flow.owner_db, flow.tenant)
    h = as_user(flow.client, flow.requester)
    _refused(request_plan(flow.client, h, flow.slug, bad["id"], release, 409))
    assert _rows(flow.owner_db, flow.tenant) == before


@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("platform", VALID)
def test_the_job_context_of_an_unreadable_target_is_refused(
    flow: Flow, app_db: Database, platform: str, how: str
) -> None:
    h = as_user(flow.client, flow.requester)
    made = register(flow.client, h, flow.slug, _body(platform, flow.workspace))
    flow.client.cookies.clear()
    job = enqueue(app_db, flow.tenant, "observe", target_id=UUID(made["id"]))
    r = lease(flow.client, flow.token)
    assert r.status_code == 200 and r.json()["job"]["id"] == str(job), r.text
    leased = r.json()

    def context() -> Any:  # Any: an httpx response
        return flow.client.get(
            job_url(job, "context"), params=proof(leased), headers=bearer(flow.token)
        )

    controlled = platform != "fabric"  # no Fabric context before SP8-5 maps its address
    if controlled:  # the same job, its target still valid
        r = context()
        assert r.status_code == 200, r.text
        assert set(r.json()["target"]["bindings"]) == set(VALID[platform])
    _break(flow.owner_db, made["id"], _stored(platform, how))
    r = context()
    assert r.status_code == 409, r.text
    _refused(r.json())
    if controlled:
        _break(flow.owner_db, made["id"], VALID[platform])
        assert context().status_code == 200


def test_the_adapter_target_of_every_platform_has_string_bindings() -> None:
    row = {"tenant_id": uuid4(), "id": uuid4(), "revision": 1, "environment": "dev"}
    for platform, bindings in VALID.items():
        stored = {
            **row,
            "platform_kind": platform,
            "address": "https://host.example.com",
            "bindings": bindings,
        }
        service.require_bindings(stored)
        mapped = service.adapter_target(stored).bindings
        assert mapped == {
            k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in bindings.items()
        }


# ------------------------------------------------------------------ approval
def _sealed(flow: Flow, platform: str) -> dict[str, Any]:
    """A sealed plan of the flow's organization on a target of `platform`."""
    if platform == "palantir":
        target = palantir_target(flow)
        return flow.sealed(target["id"], release_id=palantir_release(flow, target["id"]).id)
    if platform == "docker":
        target = docker_target(flow, workspace_host=workspace_target(flow)["address"])
        return flow.sealed(target["id"], release_id=docker_release(flow, target).id)
    return flow.sealed(flow.target()["id"])


@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("platform", ["databricks", "palantir", "docker"])
def test_an_approval_on_an_unreadable_target_is_refused_and_a_rejection_is_not(
    flow: Flow, platform: str, how: str
) -> None:
    plan = _sealed(flow, platform)
    _break(flow.owner_db, plan["target_id"], _stored(platform, how))
    before = _rows(flow.owner_db, flow.tenant)
    h = as_user(flow.client, flow.approver)
    url = f"{plans(flow.slug)}/{plan['id']}"
    r = flow.client.post(f"{url}/approve", json={"plan_digest": plan["digest"]}, headers=h)
    assert r.status_code == 409, r.text
    _refused(r.json())
    assert _rows(flow.owner_db, flow.tenant) == before
    assert plan_row(flow.owner_db, plan["id"])["status"] == "sealed"
    assert flow.client.post(f"{url}/reject", json={}, headers=h).status_code == 200


# ------------------------------------------------------------------ client A against client B
_VARYING = {"request_id"}


def _same_404(named: Any, unknown: Any) -> None:  # Any: httpx responses
    assert named.status_code == unknown.status_code == 404, named.text
    bodies = [{k: v for k, v in r.json().items() if k not in _VARYING} for r in (named, unknown)]
    assert bodies[0] == bodies[1]


def _unknown_to(flow: Flow, email: str, mine: dict[str, Any], theirs: dict[str, Any]) -> None:
    """The caller's own unreadable target first (an answer), then the other side's: the answers of
    an unknown id, never the 409, and absent from the list."""
    client, nobody = flow.client, str(uuid7())
    release = str(flow.release(mine["id"]))
    h = as_user(client, email)
    r = client.get(f"{targets(flow.slug)}/{mine['id']}", headers=h)
    assert r.status_code == 200 and r.json()["bindings"] == UNUSABLE, r.text
    _refused(request_plan(client, h, flow.slug, mine["id"], release, 409))
    for method, suffix in (("GET", ""), ("POST", "/archive")):
        _same_404(
            client.request(method, f"{targets(flow.slug)}/{theirs['id']}{suffix}", headers=h),
            client.request(method, f"{targets(flow.slug)}/{nobody}{suffix}", headers=h),
        )
    asked = {"release_id": release, "application_key": "sales-app", "display_name": "Sales app"}
    _same_404(
        client.post(plans(flow.slug), json={**asked, "target_id": theirs["id"]}, headers=h),
        client.post(plans(flow.slug), json={**asked, "target_id": nobody}, headers=h),
    )
    r = client.get(f"{targets(flow.slug)}?limit=200", headers=h)
    assert [t["id"] for t in r.json()["items"]] == [mine["id"]]


@pytest.mark.parametrize("platform", VALID)
def test_a_and_b_do_not_reach_each_others_unreadable_target(
    client: TestClient, flow: Flow, world: World, people: People, platform: str
) -> None:
    b = replace(
        flow,
        slug=world.b.slug,
        workspace=ws_slug(world.b),
        tenant=world.b.tenant,
        requester=people.owner_b,
        token=make_executor(client, world.b, people.owner_b).token,
    )
    sides = [(flow, _broken(flow, platform, "changed")), (b, _broken(b, platform, "changed"))]
    for (mine, own), (other, theirs) in (sides, reversed(sides)):
        before = _rows(flow.owner_db, other.tenant)
        _unknown_to(mine, mine.requester, own, theirs)
        client.cookies.clear()
        assert _rows(flow.owner_db, other.tenant) == before


@pytest.mark.parametrize("platform", VALID)
def test_a_p1_only_member_does_not_reach_p2s_unreadable_target(
    flow: Flow, world: World, platform: str
) -> None:
    p2 = add_project(flow.owner_db, flow.tenant, world.a.ws)
    project = _project_slug(flow.owner_db, world.a.project)
    own = _broken(flow, platform, "changed", project=project)
    theirs = _broken(flow, platform, "changed", project=f"p-{p2.hex}")
    email = project_administrator(flow.owner_db, flow.tenant, world.a.project)
    _unknown_to(flow, email, own, theirs)


def _project_slug(owner_db: Database, project: UUID) -> str:
    with owner_db.session() as o:
        slug: str = o.execute(
            text("SELECT slug FROM shifde.projects WHERE id = :p"), {"p": project}
        ).scalar_one()
    return slug


# ------------------------------------------------------------------ the bindings of a valid row
def test_every_registered_target_reads_back_its_own_bindings(flow: Flow) -> None:
    h = as_user(flow.client, flow.requester)
    for platform, bindings in VALID.items():
        made = register(flow.client, h, flow.slug, _body(platform, flow.workspace))
        shown = {k: v for k, v in made["bindings"].items() if v is not None}
        assert shown == bindings
