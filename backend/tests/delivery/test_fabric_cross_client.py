"""Fabric delivery, organization A against organization B in both directions, and project P1 against
P2 inside one organization (SP8-5, precondition F): targets, plans, deployments, artifacts, job
contexts, credential grants, checkpoints and results answer like an unknown id, the other side's
rows stay as they were, and an executor leases and reaches only its own organization's jobs and
workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any  # Any: JSON bodies, rows
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery.service import PLATFORM_ALREADY_REGISTERED
from shifde.executor.runner import Runner
from tests.board.conftest import add_project, member
from tests.connections.routes import as_user, system_role_user
from tests.delivery.conftest import make_executor
from tests.delivery.fabric import (
    approved_deployment,
    fabric_platform,
    fabric_release,
    fabric_runner,
    fabric_target,
    receipt,
    run_due,
    tenant_rows,
    workspace_of,
)
from tests.delivery.releases import SealedRelease
from tests.delivery.routes import (
    Flow,
    bearer,
    counts,
    deployments,
    job_url,
    lease,
    lease_plan,
    plan_result,
    plans,
    proof,
    request_plan,
    sp8_body,
    targets,
)
from tests.executor.support import ask_plan, sp8_credentials_file
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, project_slug, ws_slug

BOTH = [("a", "b"), ("b", "a")]
_VARYING = {"instance", "request_id"}


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    """Refused exactly like an id that does not exist: no existence hint."""
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Side:
    flow: Flow
    target: dict[str, Any]
    release: SealedRelease
    sealed: dict[str, Any]  # a sealed plan
    approved: dict[str, Any]  # another plan, approved
    deployment: str  # its queued deployment


def _flow(client: TestClient, world_client: Any, email: str, owner_db: Database) -> Flow:
    c = world_client
    return Flow(
        client,
        c.slug,
        ws_slug(c),
        c.tenant,
        email,
        system_role_user(owner_db, c.tenant, "tenant_administrator"),
        make_executor(client, c, email).token,
        owner_db,
    )


@pytest.fixture
def two(client: TestClient, world: World, people: People, owner_db: Database) -> dict[str, Side]:
    """In each organization: a Fabric target, a sealed, passed release, a sealed plan and an
    approved one with its queued deployment, all through the routes."""
    sides = {}
    for name, c, email in (("b", world.b, people.owner_b), ("a", world.a, people.owner_a)):
        flow = _flow(client, c, email, owner_db)
        target = fabric_target(flow)
        release = fabric_release(owner_db, target["id"])
        approved = flow.approve(flow.sealed(target["id"], "live-app", release_id=release.id))
        sealed = flow.sealed(target["id"], release_id=release.id)
        assert sealed["status"] == "sealed" and approved["deployment_id"]
        sides[name] = Side(flow, target, release, sealed, approved, approved["deployment_id"])
    return sides


def _plan_body(target_id: object, release_id: object) -> dict[str, Any]:
    return {
        "target_id": str(target_id),
        "release_id": str(release_id),
        "application_key": "own",
        "display_name": "Own",
    }


# ------------------------------------------------------------------ user routes
@pytest.mark.parametrize(("me", "other"), BOTH)
def test_a_cannot_reach_bs_fabric_target(
    client: TestClient, owner_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    mine, theirs = two[me], two[other]
    base = targets(mine.flow.slug)
    h = as_user(client, mine.flow.requester)
    before = tenant_rows(owner_db, theirs.flow.tenant)
    assert client.get(f"{base}/{mine.target['id']}", headers=h).status_code == 200  # own first
    for method, suffix in (("GET", ""), ("POST", "/archive")):
        _same_404(
            client.request(method, f"{base}/{theirs.target['id']}{suffix}", headers=h),
            client.request(method, f"{base}/{uuid7()}{suffix}", headers=h),
        )
    listed = client.get(f"{base}?limit=200", headers=h).json()["items"]
    assert [t["id"] for t in listed] == [mine.target["id"]]
    assert tenant_rows(owner_db, theirs.flow.tenant) == before


@pytest.mark.parametrize(("me", "other"), BOTH)
def test_a_cannot_plan_bs_fabric_target_or_release(
    client: TestClient, owner_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    mine, theirs = two[me], two[other]
    flow = mine.flow
    h = as_user(client, flow.requester)
    request_plan(
        client, h, flow.slug, mine.target["id"], mine.release.id, application_key="own"
    )  # own first
    before = tenant_rows(owner_db, theirs.flow.tenant)
    own = tenant_rows(owner_db, flow.tenant)
    nobody = str(uuid7())
    target, release = mine.target["id"], mine.release.id
    for named, unknown in (
        (_plan_body(theirs.target["id"], release), _plan_body(nobody, release)),
        (_plan_body(theirs.target["id"], theirs.release.id), _plan_body(nobody, release)),
        (_plan_body(target, theirs.release.id), _plan_body(target, nobody)),
    ):
        _same_404(
            client.post(plans(flow.slug), json=named, headers=h),
            client.post(plans(flow.slug), json=unknown, headers=h),
        )
    assert tenant_rows(owner_db, theirs.flow.tenant) == before
    assert tenant_rows(owner_db, flow.tenant) == own


@pytest.mark.parametrize(("me", "other"), BOTH)
def test_a_cannot_reach_bs_fabric_plan_or_deployment(
    client: TestClient, owner_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    mine, theirs = two[me], two[other]
    slug = mine.flow.slug
    h = as_user(client, mine.flow.requester)
    before = tenant_rows(owner_db, theirs.flow.tenant)
    nobody = str(uuid7())
    calls: list[tuple[str, str, str, str, str, Any]] = []
    for method, suffix, payload in (
        ("GET", "", None),
        ("POST", "/approve", {"plan_digest": theirs.sealed["digest"]}),
        ("POST", "/reject", {}),
    ):
        base, own_id, their_id = plans(slug), mine.sealed["id"], theirs.sealed["id"]
        calls.append((method, suffix, base, own_id, their_id, payload))
    for method, suffix, payload in (
        ("GET", "", None),
        ("POST", "/reconcile", None),
        ("POST", "/mark-failed", {"note": "intruder"}),
        ("POST", "/cancel", None),
    ):
        base = deployments(slug)
        calls.append((method, suffix, base, mine.deployment, theirs.deployment, payload))
    for method, suffix, base, own_id, their_id, payload in calls:
        sent = {"plan_digest": mine.sealed["digest"]} if suffix == "/approve" else payload
        r = client.request(method, f"{base}/{own_id}{suffix}", headers=h, json=sent)
        assert r.status_code in (200, 403, 409), (suffix, r.text)  # own object first
        _same_404(
            client.request(method, f"{base}/{their_id}{suffix}", headers=h, json=payload),
            client.request(method, f"{base}/{nobody}{suffix}", headers=h, json=payload),
        )
    listed = client.get(f"{plans(slug)}?limit=200", headers=h).json()["items"]
    assert {p["id"] for p in listed} == {mine.sealed["id"], mine.approved["id"]}
    listed = client.get(f"{deployments(slug)}?limit=200", headers=h).json()["items"]
    assert [d["id"] for d in listed] == [mine.deployment]
    by_target = client.get(plans(slug), params={"target": theirs.target["id"]}, headers=h)
    assert by_target.json()["items"] == []
    assert tenant_rows(owner_db, theirs.flow.tenant) == before


@pytest.mark.parametrize(("me", "other"), BOTH)
def test_bs_organization_paths_are_unknown_to_a_for_fabric_objects(
    client: TestClient, owner_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    mine, theirs = two[me], two[other]
    h = as_user(client, mine.flow.requester)
    unknown = f"no-such-{uuid7().hex[-12:]}"
    before = tenant_rows(owner_db, theirs.flow.tenant)
    for path in (
        f"/delivery/targets/{theirs.target['id']}",
        f"/delivery/plans/{theirs.sealed['id']}",
        f"/delivery/deployments/{theirs.deployment}",
    ):
        _same_404(
            client.get(f"/api/v1/orgs/{theirs.flow.slug}{path}", headers=h),
            client.get(f"/api/v1/orgs/{unknown}{path}", headers=h),
        )
    assert tenant_rows(owner_db, theirs.flow.tenant) == before


@pytest.mark.parametrize(("me", "other"), BOTH)
def test_shifde_app_in_as_context_selects_none_of_bs_fabric_rows(
    app_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    mine, theirs = two[me], two[other]
    with app_session(app_db, mine.flow.tenant) as s:
        for table, their_id in (
            ("delivery_targets", theirs.target["id"]),
            ("delivery_artifacts", theirs.sealed["artifact_id"]),
            ("delivery_plans", theirs.sealed["id"]),
            ("delivery_deployments", theirs.deployment),
        ):
            # the table names are the literals above
            found = s.execute(text(f"SELECT DISTINCT tenant_id FROM shifde.{table}"))  # nosec B608
            assert set(found.scalars()) == {mine.flow.tenant}, table
            n = s.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE id = :i"),  # nosec B608
                {"i": their_id},
            ).scalar_one()
            assert n == 0, table


def test_another_organization_registering_the_workspace_gets_the_fixed_text(
    client: TestClient, owner_db: Database, two: dict[str, Side]
) -> None:
    a, b = two["a"], two["b"]
    guid = workspace_of(a.target)
    before = tenant_rows(owner_db, a.flow.tenant)
    held = counts(owner_db, b.flow.tenant)
    for form in (
        guid,
        guid.upper(),
        a.target["address"],
        f"https://app.fabric.microsoft.com/groups/{guid}/lakehouses",
    ):
        h = as_user(client, b.flow.requester)
        r = client.post(
            targets(b.flow.slug), headers=h, json=sp8_body("fabric", b.flow.workspace, form)
        )
        assert r.status_code == 409, r.text
        assert (r.json()["code"], r.json()["detail"]) == (
            "platform_already_registered",
            PLATFORM_ALREADY_REGISTERED,
        )
        assert guid not in r.text and a.flow.slug not in r.text and a.target["id"] not in r.text
    assert tenant_rows(owner_db, a.flow.tenant) == before
    assert counts(owner_db, b.flow.tenant) == held


# ------------------------------------------------------------------ project P1 against P2
def test_a_p1_only_member_cannot_reach_p2s_fabric_objects(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    a = world.a
    p2 = add_project(owner_db, a.tenant, a.ws)
    flow = _flow(client, a, people.owner_a, owner_db)
    made: dict[str, dict[str, Any]] = {}
    for name, project in (("p1", project_slug(a)), ("p2", f"p-{p2.hex}")):
        target = fabric_target(flow, project=project)
        release = fabric_release(owner_db, target["id"])
        approved = flow.approve(flow.sealed(target["id"], "live-app", release_id=release.id))
        made[name] = {
            "target": target["id"],
            "plan": approved["id"],
            "deployment": approved["deployment_id"],
            "release": release.id,
        }
    email = member(
        owner_db,
        a.tenant,
        [
            "delivery:target:read",
            "delivery:target:write",
            "delivery:plan:read",
            "delivery:plan:request",
            "delivery:plan:approve",
            "delivery:deployment:read",
            "delivery:deployment:resolve",
            "packaging:solution:read",
        ],
        scope_type="project",
        scope_id=a.project,
    )
    h = as_user(client, email)
    before = tenant_rows(owner_db, a.tenant)
    nobody = str(uuid7())
    for base, key, calls in (
        (targets(a.slug), "target", (("GET", "", None), ("POST", "/archive", None))),
        (
            plans(a.slug),
            "plan",
            (
                ("GET", "", None),
                ("POST", "/approve", {"plan_digest": "a" * 64}),
                ("POST", "/reject", {}),
            ),
        ),
        (
            deployments(a.slug),
            "deployment",
            (
                ("GET", "", None),
                ("POST", "/cancel", None),
                ("POST", "/reconcile", None),
                ("POST", "/mark-failed", {"note": "intruder"}),
            ),
        ),
    ):
        assert client.get(f"{base}/{made['p1'][key]}", headers=h).status_code == 200, key
        for method, suffix, payload in calls:
            _same_404(
                client.request(
                    method, f"{base}/{made['p2'][key]}{suffix}", headers=h, json=payload
                ),
                client.request(method, f"{base}/{nobody}{suffix}", headers=h, json=payload),
            )
        listed = client.get(f"{base}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        assert [o["id"] for o in listed.json()["items"]] == [made["p1"][key]]
    own_release = made["p1"]["release"]
    _same_404(
        client.post(plans(a.slug), json=_plan_body(made["p2"]["target"], own_release), headers=h),
        client.post(plans(a.slug), json=_plan_body(nobody, own_release), headers=h),
    )
    _same_404(
        client.post(
            plans(a.slug), json=_plan_body(made["p1"]["target"], made["p2"]["release"]), headers=h
        ),
        client.post(plans(a.slug), json=_plan_body(made["p1"]["target"], nobody), headers=h),
    )
    assert tenant_rows(owner_db, a.tenant) == before


# ------------------------------------------------------------------ executors
@pytest.mark.parametrize(("me", "other"), BOTH)
def test_an_executor_reaches_none_of_the_others_fabric_job(
    client: TestClient, owner_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    """Context, artifact, grant, heartbeat, checkpoint and result of the other organization's
    leased Fabric apply job, with its real lease proof: the 404 an unknown job gets."""
    mine, theirs = two[me], two[other]
    r = lease(client, theirs.flow.token, kinds=["apply"])
    assert r.status_code == 200, r.text
    leased = r.json()
    assert leased["job"]["deployment_id"] == theirs.deployment
    p = proof(leased)
    value = receipt(theirs.flow.tenant, theirs.target, theirs.approved["artifact_digest"])
    step = {
        **p,
        "sequence": 1,
        "step_key": "lakehouse-metadata-submit",
        "state": "intent",
        "input_digest": "c" * 64,
    }
    before = tenant_rows(owner_db, theirs.flow.tenant)
    for method, action, options in (
        ("GET", "context", {"params": p}),
        ("GET", "artifact", {"params": p}),
        ("POST", "credential-grant", {"json": p}),
        ("POST", "heartbeat", {"json": p}),
        ("POST", "checkpoint", {"json": step}),
        ("POST", "result", {"json": {**p, "status": "succeeded", "value": value}}),
        ("POST", "result", {"json": {**p, "status": "failed", "error_code": "foreign_item"}}),
    ):
        h = bearer(mine.flow.token)
        _same_404(
            client.request(method, job_url(leased["job"]["id"], action), headers=h, **options),
            client.request(method, job_url(uuid7(), action), headers=h, **options),
        )
    assert tenant_rows(owner_db, theirs.flow.tenant) == before
    # the owner's executor gets its own target and artifact
    h = bearer(theirs.flow.token)
    ctx = client.get(job_url(leased["job"]["id"], "context"), params=p, headers=h)
    assert ctx.status_code == 200, ctx.text
    assert ctx.json()["target"]["bindings"]["workspace"] == workspace_of(theirs.target)
    assert workspace_of(mine.target) not in ctx.text
    content = client.get(job_url(leased["job"]["id"], "artifact"), params=p, headers=h)
    assert content.headers["x-artifact-digest"] == theirs.approved["artifact_digest"]


@pytest.mark.parametrize(("me", "other"), BOTH)
def test_an_apply_receipt_naming_the_others_workspace_is_refused(
    client: TestClient, owner_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    mine, theirs = two[me], two[other]
    leased = lease(client, mine.flow.token, kinds=["apply"]).json()
    value = receipt(mine.flow.tenant, mine.target, mine.approved["artifact_digest"])
    before = tenant_rows(owner_db, theirs.flow.tenant)
    own = tenant_rows(owner_db, mine.flow.tenant)
    h = bearer(mine.flow.token)
    for changes in (
        {"native_id": f"fabric:{workspace_of(theirs.target)}:{uuid4()}"},
        {"target_id": theirs.target["id"]},
    ):
        sent = {**value, "resources": [{**value["resources"][0], **changes}]}
        r = client.post(
            job_url(leased["job"]["id"], "result"),
            json={**proof(leased), "status": "succeeded", "value": sent},
            headers=h,
        )
        assert r.status_code == 422, r.text
        assert workspace_of(theirs.target) not in r.text
    assert tenant_rows(owner_db, theirs.flow.tenant) == before
    assert tenant_rows(owner_db, mine.flow.tenant) == own


@pytest.mark.parametrize(("me", "other"), BOTH)
def test_an_executor_leases_none_of_the_others_fabric_jobs(
    client: TestClient, owner_db: Database, two: dict[str, Side], me: str, other: str
) -> None:
    mine, theirs = two[me], two[other]
    before = tenant_rows(owner_db, theirs.flow.tenant)
    r = lease(client, mine.flow.token)
    assert r.status_code == 200 and r.json()["job"]["deployment_id"] == mine.deployment, r.text
    assert lease(client, mine.flow.token).status_code == 204  # theirs is due, and not leased
    assert tenant_rows(owner_db, theirs.flow.tenant) == before


def _runner(side: Flow, target: dict[str, Any], directory: Path, platform: Any) -> Runner:
    directory.mkdir(exist_ok=True)
    creds = sp8_credentials_file(directory, "fabric", target["address"], tenant=side.tenant)
    return fabric_runner(side.client, side.token, creds, platform)


def test_two_organizations_deploy_side_by_side_to_their_own_workspaces(
    client: TestClient, world: World, people: People, owner_db: Database, tmp_path: Path
) -> None:
    """One fake platform, two executors: each deployment's items are in its own workspace only,
    and each executor leases nothing while only the other's jobs are due."""
    fake = FakeFabricWorkspace()
    platform = fabric_platform(fake)
    sides: list[tuple[Flow, dict[str, Any], Runner]] = []
    for name, c, email in (("a", world.a, people.owner_a), ("b", world.b, people.owner_b)):
        flow = _flow(client, c, email, owner_db)
        target = fabric_target(flow)
        sides.append((flow, target, _runner(flow, target, tmp_path / name, platform)))
    for number, (flow, target, runner) in enumerate(sides):
        _, other_target, other_runner = sides[1 - number]
        release = fabric_release(owner_db, target["id"])
        there = workspace_of(other_target)
        items = dict(fake.items.get(there, {}))
        ask_plan(flow, target["id"], release_id=release.id)
        assert not other_runner.run_once()  # this side's plan job is due
        assert runner.run_once()
        deployment = approved_deployment(flow, runner, target, release.id)
        assert not other_runner.run_once()  # this side's apply job is due
        run_due(flow, runner, deployment, "apply", "observe", "verify")
        assert flow.get(deployment)["status"] == "verified"
        assert fake.items.get(there, {}) == items
        paths = [r[2] for r in fake.writes if there in r[2]]
        assert len(paths) == (0 if number == 0 else len(fake.writes) // 2)


def test_a_credentials_entry_of_the_other_organization_reaches_no_workspace(
    client: TestClient, world: World, people: People, owner_db: Database, tmp_path: Path
) -> None:
    """A's executor with a file naming B's organization, or B's workspace under A's reference: the
    plan fails `credential_unavailable` and the platform gets no request."""
    fake = FakeFabricWorkspace()
    a = _flow(client, world.a, people.owner_a, owner_db)
    b = _flow(client, world.b, people.owner_b, owner_db)
    target, b_target = fabric_target(a), fabric_target(b)
    release = fabric_release(owner_db, target["id"])
    for name, address, tenant in (
        ("tenant", target["address"], b.tenant),
        ("workspace", b_target["address"], a.tenant),
    ):
        (tmp_path / name).mkdir()
        creds = sp8_credentials_file(tmp_path / name, "fabric", address, tenant=tenant)
        runner = fabric_runner(client, a.token, creds, fabric_platform(fake))
        plan = ask_plan(a, target["id"], release_id=release.id)
        assert runner.run_once()
        failed = a.get_plan(plan["id"])
        assert failed["status_reason"] == "plan job failed: credential_unavailable", failed
    assert fake.requests == []


def test_a_plan_snapshot_naming_the_others_items_is_not_adopted(
    client: TestClient, owner_db: Database, two: dict[str, Side]
) -> None:
    """A's plan result claiming B's native items (or any the target does not own) fails the plan."""
    a, b = two["a"], two["b"]
    plan = ask_plan(a.flow, a.target["id"], release_id=a.release.id)
    leased = lease_plan(client, a.flow.token)
    resource = receipt(b.flow.tenant, b.target, "c" * 64)["resources"][0]
    before = tenant_rows(owner_db, b.flow.tenant)
    value = {
        "steps": [
            {"key": "fabric-finalize", "action": "update", "resource_key": "notebook"},
        ],
        "snapshot": {"resources": [resource], "fingerprint": "d" * 64},
        "adapter_version": "0.1.0",
        "toolchain_digest": "c" * 64,
    }
    r = plan_result(client, a.flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    assert a.flow.get_plan(plan["id"])["status"] == "plan_failed"
    assert tenant_rows(owner_db, b.flow.tenant) == before
