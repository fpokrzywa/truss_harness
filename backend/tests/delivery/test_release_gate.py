"""The release gate of a plan (SP3 design sections 4 and 6, settled calls 3 to 6; issue #81): a
plan request names a sealed release the caller can read, of the target's kind and scope, with a
passing evaluation of its exact digests; a refused request writes nothing. An accepted plan
stores the release, its digest and the passing run, and its artifact carries `release.json`.
Approval checks the release again under the plan row lock; reject does not.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, get_args  # Any: JSON bodies, rows
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery import plans as plan_service
from shifde.delivery.schemas import PlatformKind
from shifde.packaging.canonical import canonical
from shifde.packaging.schemas import TARGET_KINDS
from tests.connections.routes import as_user
from tests.delivery.releases import SealedRelease, insert_run, make_release, withdraw
from tests.delivery.routes import (
    Flow,
    address,
    plan_row,
    plans,
    register,
    request_plan,
    target_body,
)
from tests.identity import harness
from tests.tenancy.conftest import World, owner_member
from tests.tenancy.routes import People, project_slug, ws_slug

REQUIRED = "This release has no passing evaluation."


def _counts(owner_db: Database, tenant: UUID) -> tuple[int, int, int]:
    """(plans, artifacts, jobs) of `tenant`, read as the schema owner."""
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_plans WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_artifacts WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t)"
            ),
            {"t": tenant},
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


def _ask(flow: Flow, target_id: object, release_id: object) -> Any:  # Any: an httpx response
    h = as_user(flow.client, flow.requester)
    body = {
        "target_id": str(target_id),
        "release_id": str(release_id),
        "application_key": "sales-app",
        "display_name": "Sales app",
    }
    r = flow.client.post(plans(flow.slug), json=body, headers=h)
    flow.client.cookies.clear()
    return r


def _refused(flow: Flow, target_id: object, release_id: object, status: int, code: str) -> Any:
    """The request is refused with `status` and `code`, and writes no plan, artifact or job."""
    before = _counts(flow.owner_db, flow.tenant)
    r = _ask(flow, target_id, release_id)
    assert r.status_code == status and r.json()["code"] == code, r.text
    assert _counts(flow.owner_db, flow.tenant) == before
    return r.json()


def _project(owner_db: Database, tenant: UUID, workspace_id: UUID) -> UUID:
    project_id = uuid7()
    with owner_db.session() as o:
        o.execute(
            text(
                "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
                "VALUES (:i, :t, :w, :s, :s)"
            ),
            {"i": project_id, "t": tenant, "w": workspace_id, "s": f"p-{project_id.hex}"},
        )
        o.commit()
    return project_id


# ------------------------------------------------------------------ request
def test_a_request_without_a_release_id_is_422(flow: Flow) -> None:
    target = flow.target()
    before = _counts(flow.owner_db, flow.tenant)
    h = as_user(flow.client, flow.requester)
    body = {"target_id": target["id"], "application_key": "sales-app", "display_name": "Sales"}
    r = flow.client.post(plans(flow.slug), json=body, headers=h)
    assert r.status_code == 422 and r.json()["code"] == "validation_error", r.text
    assert [e["loc"] for e in r.json()["errors"]] == [["body", "release_id"]]
    assert _counts(flow.owner_db, flow.tenant) == before


def test_a_release_without_a_passing_evaluation_is_refused(flow: Flow) -> None:
    """None, only a failed one, a passed one of another dataset digest: 409
    `evaluation_required`, and nothing is written."""
    target = flow.target()
    rel = flow.release(target["id"], evaluation=None)
    released = _record(flow, rel)
    for status, dataset_digest in (
        (None, None),
        ("failed", released.dataset_digest),
        ("passed", "0" * 64),
    ):
        if status is not None:
            insert_run(flow.owner_db, released, status, str(dataset_digest))
        body = _refused(flow, target["id"], rel, 409, "evaluation_required")
        assert body["detail"] == REQUIRED
    run_id = insert_run(flow.owner_db, released, "passed", released.dataset_digest)
    r = _ask(flow, target["id"], rel)
    assert r.status_code == 201, r.text
    assert r.json()["evaluation_run_id"] == str(run_id)


def _record(flow: Flow, release_id: UUID) -> SealedRelease:
    """The `SealedRelease` of an existing release, read back as the schema owner."""
    with flow.owner_db.session() as o:
        row = (
            o.execute(
                text("SELECT tenant_id, id, digest, manifest FROM shifde.releases WHERE id = :r"),
                {"r": release_id},
            )
            .mappings()
            .one()
        )
    manifest = row["manifest"]
    evaluation = manifest["evaluation"]
    return SealedRelease(
        row["tenant_id"],
        row["id"],
        row["digest"],
        manifest,
        UUID(evaluation["dataset_id"]),
        evaluation["dataset_digest"],
        None,
    )


def test_a_withdrawn_release_is_refused(flow: Flow) -> None:
    target = flow.target()
    rel = flow.release(target["id"], withdrawn=True)
    body = _refused(flow, target["id"], rel, 409, "release_withdrawn")
    assert body["detail"] == "The release is withdrawn."


def test_a_release_outside_the_targets_workspace_or_project_is_refused(
    client: TestClient, world: World, people: People, flow: Flow
) -> None:
    detail = "The release is not in the target's workspace and project."
    workspace_wide = flow.target()
    other_ws = make_release(flow.owner_db, world.a.tenant, world.a.ws2)
    body = _refused(flow, workspace_wide["id"], other_ws.id, 409, "release_target_mismatch")
    assert body["detail"] == detail
    # a project's target takes a release of its own project only
    h = as_user(client, people.owner_a)
    in_project = register(
        client,
        h,
        world.a.slug,
        target_body(ws_slug(world.a), address(), project=project_slug(world.a)),
    )
    client.cookies.clear()
    sibling = _project(flow.owner_db, world.a.tenant, world.a.ws)
    other_project = make_release(flow.owner_db, world.a.tenant, world.a.ws, sibling)
    body = _refused(flow, in_project["id"], other_project.id, 409, "release_target_mismatch")
    assert body["detail"] == detail
    # a workspace-wide target takes a release of any project of its workspace
    assert _ask(flow, workspace_wide["id"], other_project.id).status_code == 201
    own = make_release(flow.owner_db, world.a.tenant, world.a.ws, world.a.project)
    assert _ask(flow, in_project["id"], own.id).status_code == 201


def test_the_target_kind_must_be_the_releases() -> None:
    kinds = {"databricks.app/v1": "databricks", "snowflake.native-app/v1": "snowflake"}
    for kind, platform in kinds.items():
        plan_service.check_target_kind({"target_kind": kind}, platform)
    refusals: list[tuple[dict[str, Any], str]] = [
        ({"target_kind": kind}, other)
        for kind, platform in kinds.items()
        for other in ("databricks", "snowflake", "fabric")
        if other != platform
    ]
    for manifest in ({"target_kind": "databricks.job/v1"}, {"target_kind": ["x"]}, {}):
        refusals += [(manifest, platform) for platform in ("databricks", "snowflake")]
    for manifest, platform in refusals:
        with pytest.raises(plan_service.TargetKindMismatch) as refused:
            plan_service.check_target_kind(manifest, platform)
        assert (refused.value.status, refused.value.code) == (409, "target_kind_mismatch")
        assert refused.value.detail == "The release is for another kind of target."


PLANNABLE = {
    ("databricks.app/v1", "databricks"),
    ("snowflake.native-app/v1", "snowflake"),
    ("palantir.marketplace/v1", "palantir"),
}


def test_the_plan_gate_cases_are_five_kinds_by_five_platforms() -> None:
    assert (len(TARGET_KINDS), len(get_args(PlatformKind))) == (5, 5)
    assert {kind for kind, _ in PLANNABLE} < set(TARGET_KINDS)


@pytest.mark.parametrize("platform", get_args(PlatformKind))
@pytest.mark.parametrize("kind", TARGET_KINDS)
def test_only_a_databricks_snowflake_or_palantir_release_is_planned_on_its_own_platform(
    kind: str, platform: str
) -> None:
    """SP8-1: a Docker release is refused everywhere, also on the Databricks target whose data
    adapter it uses (`TARGET_ADAPTERS` is not the deployment platform)."""
    if (kind, platform) in PLANNABLE:
        plan_service.check_target_kind({"target_kind": kind}, platform)
        return
    with pytest.raises(plan_service.TargetKindMismatch) as refused:
        plan_service.check_target_kind({"target_kind": kind}, platform)
    assert (refused.value.status, refused.value.code) == (409, "target_kind_mismatch")
    assert refused.value.detail == "The release is for another kind of target."


def test_a_release_the_caller_cannot_read_is_an_unknown_one(
    client: TestClient, world: World, people: People, flow: Flow
) -> None:
    """A developer of one project requests on that project's target, naming a release of a
    sibling project they cannot read: the 404 an unknown release id gets."""
    h = as_user(client, people.owner_a)
    target = register(
        client,
        h,
        world.a.slug,
        target_body(ws_slug(world.a), address(), project=project_slug(world.a)),
    )
    client.cookies.clear()
    sibling = _project(flow.owner_db, world.a.tenant, world.a.ws)
    hidden = make_release(flow.owner_db, world.a.tenant, world.a.ws, sibling)
    email = f"project-dev-{uuid7().hex[-12:]}@route.test"
    with flow.owner_db.session() as o:
        uid = harness.person(o, email)
        owner_member(o, world.a.tenant, uid)
        o.execute(
            text(
                "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                "role_id, scope_type, scope_id) SELECT :i, :t, 'user', :u, r.id, 'project', :p "
                "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = 'developer'"
            ),
            {"i": uuid7(), "t": world.a.tenant, "u": uid, "p": world.a.project},
        )
        o.commit()
    developer = Flow(
        client,
        flow.slug,
        flow.workspace,
        flow.tenant,
        email,
        flow.approver,
        flow.token,
        flow.owner_db,
    )
    named = _refused(developer, target["id"], hidden.id, 404, "not_found")
    unknown = _refused(developer, target["id"], uuid7(), 404, "not_found")
    assert named["detail"] == "release not found"
    assert {k: v for k, v in named.items() if k != "request_id"} == {
        k: v for k, v in unknown.items() if k != "request_id"
    }
    own = make_release(flow.owner_db, world.a.tenant, world.a.ws, world.a.project)
    assert _ask(developer, target["id"], own.id).status_code == 201  # readable: accepted


def test_an_accepted_plan_stores_the_release_and_its_artifact_carries_it(flow: Flow) -> None:
    target = flow.target()
    rel = _record(flow, flow.release(target["id"]))
    with flow.owner_db.session() as o:
        run_id = o.execute(
            text("SELECT id FROM shifde.evaluation_runs WHERE release_id = :r"), {"r": rel.id}
        ).scalar_one()
    h = as_user(flow.client, flow.requester)
    made = request_plan(flow.client, h, flow.slug, target["id"], rel.id)
    flow.client.cookies.clear()
    row = plan_row(flow.owner_db, made["id"])
    assert (row["release_id"], row["release_digest"], row["evaluation_run_id"]) == (
        rel.id,
        rel.digest,
        run_id,
    )
    out = flow.get_plan(made["id"])
    assert (out["release_id"], out["release_digest"], out["evaluation_run_id"]) == (
        str(rel.id),
        rel.digest,
        str(run_id),
    )
    with flow.owner_db.session() as o:
        content = o.execute(
            text("SELECT content FROM shifde.delivery_artifacts WHERE id = :a"),
            {"a": made["artifact_id"]},
        ).scalar_one()
    with zipfile.ZipFile(io.BytesIO(bytes(content))) as archive:
        stored = archive.read("release.json")
    assert json.loads(stored) == {"manifest": rel.manifest, "digest": rel.digest}
    assert stored == canonical({"manifest": rel.manifest, "digest": rel.digest})


# ------------------------------------------------------------------ approval re-check
def _approve(flow: Flow, plan: dict[str, Any]) -> Any:  # Any: an httpx response
    h = as_user(flow.client, flow.approver)
    r = flow.client.post(
        f"{plans(flow.slug)}/{plan['id']}/approve",
        json={"plan_digest": plan["digest"]},
        headers=h,
    )
    flow.client.cookies.clear()
    return r


def _nothing_decided(owner_db: Database, plan_id: object) -> None:
    assert plan_row(owner_db, plan_id)["status"] == "sealed"
    with owner_db.session() as o:
        n = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_approvals WHERE plan_id = :p) + "
                "(SELECT count(*) FROM shifde.delivery_deployments WHERE plan_id = :p) + "
                "(SELECT count(*) FROM shifde.delivery_jobs WHERE plan_id = :p AND kind = 'apply')"
            ),
            {"p": plan_id},
        ).scalar_one()
    assert n == 0


def test_approval_after_the_release_is_withdrawn_is_refused_and_reject_still_works(
    flow: Flow,
) -> None:
    target = flow.target()
    rel = flow.release(target["id"])
    plan = flow.sealed(target["id"], release_id=rel)
    withdraw(flow.owner_db, rel)
    r = _approve(flow, plan)
    assert r.status_code == 409 and r.json()["code"] == "release_withdrawn", r.text
    _nothing_decided(flow.owner_db, plan["id"])
    h = as_user(flow.client, flow.approver)
    rejected = flow.client.post(f"{plans(flow.slug)}/{plan['id']}/reject", json={}, headers=h)
    flow.client.cookies.clear()
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected", rejected.text


def test_approval_of_a_plan_without_a_release_is_refused(flow: Flow) -> None:
    target = flow.target()
    plan = flow.sealed(target["id"])
    with flow.owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.delivery_plans SET release_id = NULL, release_digest = NULL, "
                "evaluation_run_id = NULL WHERE id = :p"
            ),
            {"p": plan["id"]},
        )
        o.commit()
    r = _approve(flow, plan)
    assert r.status_code == 409 and r.json()["code"] == "evaluation_required", r.text
    assert r.json()["detail"] == REQUIRED
    _nothing_decided(flow.owner_db, plan["id"])
    assert flow.get_plan(plan["id"])["release_id"] is None


def test_approval_of_a_passed_unchanged_release_creates_the_deployment(flow: Flow) -> None:
    target = flow.target()
    plan = flow.sealed(target["id"])
    r = _approve(flow, plan)
    assert r.status_code == 200 and r.json()["deployment_id"] is not None, r.text
