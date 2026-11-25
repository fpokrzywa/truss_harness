"""Palantir plans and deployments on the server (SP8-7): the descriptor artifact and its bindings
digest, the preview and generated-app refusals (nothing written), the checks that verify, the
`plan_stale` apply result, and the public `frontend_origin` rule at registration. The executor half
is `tests/executor/test_runner_palantir.py`; isolation is `test_routes_cross_client.py`."""

from __future__ import annotations

import json
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from shifde.adapters.native import planning
from shifde.adapters.native.base import read_archive
from shifde.adapters.native.contracts import Check, JobContext, Target
from shifde.adapters.native.palantir import source as palantir_source
from shifde.core.db import Database
from shifde.delivery import service
from shifde.packaging.canonical import canonical
from shifde.packaging.service import COMPILER_VERSION_APP
from tests.board.conftest import add_project
from tests.connections.routes import as_user
from tests.delivery.palantir import (
    CHECKS,
    DEPLOYABLE,
    RESOURCE,
    palantir_release,
    palantir_target,
    project_administrator,
)
from tests.delivery.releases import make_release
from tests.delivery.routes import (
    BOTH_CHECKS,
    Flow,
    bearer,
    binding,
    checkpoint,
    counts,
    deployment_row,
    job_result,
    job_url,
    lease_kind,
    lease_plan,
    make_due,
    plan_row,
    plans,
    proof,
    receipt_value,
    request_plan,
    sp8_address,
    sp8_body,
    targets,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import project_slug

NO_PREVIEW = {**DEPLOYABLE, "allow_preview_apis": False}


def _written(owner_db: Database, tenant: UUID) -> tuple[int, ...]:
    """(artifacts, plans, jobs, audit events) of `tenant`, read as the schema owner."""
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_artifacts WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_plans WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t)"
            ),
            {"t": tenant},
        ).one()
    return tuple(int(n) for n in row)


def _ask(
    flow: Flow, target_id: object, release_id: object, email: str | None = None
) -> Any:  # Any: an httpx response
    """A plan request, and how many rows it wrote (`_written` after minus before the request)."""
    h = as_user(flow.client, email or flow.requester)
    before = _written(flow.owner_db, flow.tenant)
    r = flow.client.post(
        plans(flow.slug),
        json={
            "target_id": str(target_id),
            "release_id": str(release_id),
            "application_key": "sales-app",
            "display_name": "Sales app",
        },
        headers=h,
    )
    flow.client.cookies.clear()
    assert r.status_code != 201 and _written(flow.owner_db, flow.tenant) == before, r.text
    return r


# ------------------------------------------------------------------ the artifact
def test_a_palantir_plans_artifact_is_the_descriptor_and_the_release(
    flow: Flow, owner_db: Database
) -> None:
    """Exactly `deployment.json` and `release.json`; the descriptor's bindings digest is the digest
    of the target the executor's context carries, and the sealed digest covers the artifact."""
    target = palantir_target(flow)
    release = palantir_release(flow, target["id"])
    h = as_user(flow.client, flow.requester)
    made = request_plan(flow.client, h, flow.slug, target["id"], release.id)
    flow.client.cookies.clear()
    leased = lease_plan(flow.client, flow.token)
    assert leased["job"]["plan_id"] == made["id"]
    got = flow.client.get(
        job_url(leased["job"]["id"], "artifact"), params=proof(leased), headers=bearer(flow.token)
    )
    assert got.status_code == 200
    files = read_archive(got.content)
    assert set(files) == {"deployment.json", "release.json"}
    r = flow.client.get(
        job_url(leased["job"]["id"], "context"), params=proof(leased), headers=bearer(flow.token)
    )
    assert r.status_code == 200, r.text
    ctx = JobContext.model_validate(r.json())
    assert ctx.platform_kind == "palantir" and ctx.target.revision == target["revision"]
    assert ctx.target.bindings == {**DEPLOYABLE, "allow_preview_apis": "true"}
    descriptor = json.loads(files["deployment.json"])
    assert descriptor == {
        "spec": {"kind": "palantir.marketplace/v1", "frontend_asset_path": "index.html"},
        "bindings_digest": planning.bindings_digest(ctx.target),
        "release_digest": release.digest,
    }
    assert files["release.json"] == canonical(
        {"manifest": release.manifest, "digest": release.digest}
    )
    row = plan_row(owner_db, made["id"])
    assert ctx.plan is not None and row["artifact_digest"] == ctx.plan.artifact_digest
    assert row["release_digest"] == release.digest


def test_only_a_palantir_targets_bindings_are_mapped() -> None:
    row = {
        "tenant_id": uuid4(),
        "id": uuid4(),
        "revision": 3,
        "environment": "prod",
        "address": "https://stack-1.palantirfoundry.com",
        "platform_kind": "palantir",
    }
    for stored, text_value in ((True, "true"), (False, "false")):
        bindings = {"version_id": "x", "allow_preview_apis": stored}
        mapped = service.adapter_target({**row, "bindings": bindings})
        assert mapped == Target(
            tenant_id=row["tenant_id"],
            id=row["id"],
            revision=3,
            environment="prod",
            host="https://stack-1.palantirfoundry.com",
            bindings={"version_id": "x", "allow_preview_apis": text_value},
        )
    for kind in ("databricks", "snowflake", "fabric", "docker"):
        plain = {**row, "platform_kind": kind, "bindings": {"name": "unchanged"}}
        assert service.adapter_target(plain).bindings == {"name": "unchanged"}


# ------------------------------------------------------------------ refusals at the plan request
def test_preview_off_refuses_a_palantir_plan_without_a_write(flow: Flow) -> None:
    target = palantir_target(flow, bindings=NO_PREVIEW)
    release = palantir_release(flow, target["id"])
    r = _ask(flow, target["id"], release.id)
    assert r.status_code == 409 and r.json()["code"] == "palantir_preview_not_allowed", r.text
    assert r.json()["detail"] == "The target does not allow Foundry preview APIs."
    # an evaluation that has not passed is refused first
    ungated = palantir_release(flow, target["id"], evaluation=None)
    assert _ask(flow, target["id"], ungated.id).json()["code"] == "evaluation_required"


def test_a_generated_app_release_is_refused_on_a_palantir_target(flow: Flow) -> None:
    target = palantir_target(flow)
    release = palantir_release(
        flow,
        target["id"],
        extra={
            "compiler_version": COMPILER_VERSION_APP,
            "app": {"generator_version": "shifde.appgen/1.0"},
        },
    )
    r = _ask(flow, target["id"], release.id)
    assert r.status_code == 409 and r.json()["code"] == "generator_version_unsupported", r.text


def test_a_palantir_target_and_a_databricks_release_do_not_mix(flow: Flow) -> None:
    target = palantir_target(flow)
    databricks = flow.release(target["id"])
    assert _ask(flow, target["id"], databricks).json()["code"] == "target_kind_mismatch"
    other = flow.target()
    marketplace = palantir_release(flow, other["id"])
    assert _ask(flow, other["id"], marketplace.id).json()["code"] == "target_kind_mismatch"


def test_a_preview_off_palantir_target_out_of_the_callers_project_is_404(
    flow: Flow, world: World, owner_db: Database
) -> None:
    """404 as for an unknown id, never the 409, for another project's target with preview off (the
    other client's is in `test_routes_cross_client.py`)."""
    p2 = add_project(owner_db, flow.tenant, world.a.ws)
    theirs = palantir_target(flow, bindings=NO_PREVIEW, project=f"p-{p2.hex}")
    mine = palantir_target(flow, bindings=NO_PREVIEW, project=project_slug(world.a))
    email = project_administrator(owner_db, flow.tenant, world.a.project)
    own = make_release(
        owner_db,
        flow.tenant,
        world.a.ws,
        world.a.project,
        target_kind="palantir.marketplace/v1",
    )
    assert _ask(flow, mine["id"], own.id, email).json()["code"] == "palantir_preview_not_allowed"
    # the hidden target, then an unknown id: the same answer (the denial's own decision record is
    # written for both alike, so only the plan rows are compared)
    before = _written(owner_db, flow.tenant)[:3]
    h = as_user(flow.client, email)
    answers = []
    for target_id in (theirs["id"], str(uuid4())):
        r = flow.client.post(
            plans(flow.slug),
            json={
                "target_id": target_id,
                "release_id": str(own.id),
                "application_key": "sales-app",
                "display_name": "Sales app",
            },
            headers=h,
        )
        assert r.status_code == 404, r.text
        answers.append({k: v for k, v in r.json().items() if k not in ("instance", "request_id")})
    flow.client.cookies.clear()
    assert answers[0] == answers[1] and "preview" not in str(answers)
    assert _written(owner_db, flow.tenant)[:3] == before


# ------------------------------------------------------------------ frontend_origin
BAD_ORIGINS = (
    "docker://0a1b2c3d-0000-4000-8000-00000000abcd",
    "https://127.0.0.1",
    "https://[::1]",
    "https://2130706433",
    "https://127.1",
    "https://0x7f.0.0.1",
    "https://localhost",
    "https://app.localhost",
    "https://host",
    "https://app.internal",
    "https://app.local",
    "https://frontend.example.com:8443",
    "https://u:p@frontend.example.com",
    "https://frontend.example.com.",
)


def test_a_palantir_target_with_a_non_public_frontend_origin_is_refused(
    flow: Flow, owner_db: Database
) -> None:
    h = as_user(flow.client, flow.requester)
    for origin in BAD_ORIGINS:
        body = sp8_body(
            "palantir",
            flow.workspace,
            sp8_address("palantir"),
            bindings={**DEPLOYABLE, "frontend_origin": origin},
        )
        r = flow.client.post(targets(flow.slug), headers=h, json=body)
        assert r.status_code == 422, (origin, r.text)
        at = ["body", "bindings", "PalantirBindings", "frontend_origin"]
        assert [e["msg"] for e in r.json()["errors"] if e["loc"] == at] == [
            "Value error, frontend_origin must be a public HTTPS origin"
        ], (origin, r.text)
        assert origin not in r.text
    assert counts(owner_db, flow.tenant) == (0, 0)


def test_a_palantir_targets_frontend_origin_is_stored_normalized(flow: Flow) -> None:
    made = palantir_target(
        flow, bindings={**DEPLOYABLE, "frontend_origin": "https://Frontend.Example.com:443/"}
    )
    assert made["bindings"]["frontend_origin"] == "https://frontend.example.com"


# ------------------------------------------------------------------ verify and apply results
def _verifying(flow: Flow, owner_db: Database) -> tuple[str, dict[str, Any]]:
    """A Palantir deployment in `verifying` and its leased verify job."""
    target = palantir_target(flow)
    release = palantir_release(flow, target["id"])
    approved = flow.approve(flow.sealed(target["id"], release_id=release.id))
    deployment = approved["deployment_id"]
    flow.apply(approved)
    make_due(owner_db, deployment)
    observed = lease_kind(flow.client, flow.token, "observe")
    r = job_result(
        flow.client, flow.token, observed, status="succeeded", value={"state": "succeeded"}
    )
    assert r.status_code == 200, r.text
    make_due(owner_db, deployment)
    return deployment, lease_kind(flow.client, flow.token, "verify")


CONTENT = {"name": "content-check", "status": "not_run", "required": False}
ALL_PASSED = [{"name": name, "status": "passed"} for name in CHECKS]


def _verified(flow: Flow, owner_db: Database, checks: list[dict[str, Any]]) -> Any:  # Any: a row
    deployment, leased = _verifying(flow, owner_db)
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": checks})
    assert r.status_code == 200, r.text
    return deployment_row(owner_db, deployment)


def test_the_three_palantir_checks_verify_and_the_not_run_content_check_is_stored(
    flow: Flow, owner_db: Database
) -> None:
    row = _verified(flow, owner_db, [*ALL_PASSED, CONTENT])
    assert row["status"] == "verified"
    stored = {c["name"]: c["status"] for c in row["verification"]["checks"]}
    assert stored == {**dict.fromkeys(CHECKS, "passed"), "content-check": "not_run"}


@pytest.mark.parametrize(
    ("name", "status"),
    [(name, status) for name in CHECKS for status in ("missing", "failed")]
    + [("ontology-read", "skipped"), ("frontend-asset", "not_run")],
)
def test_a_palantir_deployment_without_one_required_check_is_not_verified(
    flow: Flow, owner_db: Database, name: str, status: str
) -> None:
    checks = [c for c in ALL_PASSED if c["name"] != name] + [CONTENT]
    if status != "missing":
        checks.append({"name": name, "status": status})
    row = _verified(flow, owner_db, checks)
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")


@pytest.mark.parametrize(
    "checks",
    [
        [CONTENT],
        [{"name": "content-check", "status": "passed"}],
        [{"name": "native-version", "status": "passed"}, *BOTH_CHECKS],
    ],
    ids=["not-run", "passed", "databricks"],
)
def test_a_lone_content_check_or_another_platforms_checks_never_verify_a_palantir_deployment(
    flow: Flow, owner_db: Database, checks: list[dict[str, Any]]
) -> None:
    row = _verified(flow, owner_db, checks)
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")


def test_a_palantir_plan_of_a_generated_app_release_never_verifies(
    flow: Flow, owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment, leased = _verifying(flow, owner_db)
    # a sealed release cannot be changed: only its reading here
    monkeypatch.setattr(service, "app_generator_version", lambda _manifest: "shifde.appgen/1.0")
    r = job_result(
        flow.client, flow.token, leased, status="succeeded", value={"checks": ALL_PASSED}
    )
    assert r.status_code == 200, r.text
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")


def test_the_check_status_accepts_not_run_and_nothing_else_new_palantir() -> None:
    assert Check(name="content-check", status="not_run").status == "not_run"
    for status in ("unknown", "notrun", "not_run ", ""):
        with pytest.raises(ValueError):
            Check(name="content-check", status=status)


def _approved(flow: Flow) -> dict[str, Any]:  # Any: the approved PlanOut body
    target = palantir_target(flow)
    return flow.approve(
        flow.sealed(target["id"], release_id=palantir_release(flow, target["id"]).id)
    )


def test_a_plan_stale_palantir_apply_without_a_step_is_failed_with_that_reason(
    flow: Flow, owner_db: Database
) -> None:
    deployment = _approved(flow)["deployment_id"]
    leased = lease_kind(flow.client, flow.token, "apply")
    r = job_result(flow.client, flow.token, leased, status="failed", error_code="plan_stale")
    assert r.status_code == 200, r.text
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "apply refused: plan_stale")


def test_a_plan_stale_palantir_apply_after_a_step_is_an_unknown_outcome(
    flow: Flow, owner_db: Database
) -> None:
    deployment = _approved(flow)["deployment_id"]
    leased = lease_kind(flow.client, flow.token, "apply")
    assert checkpoint(flow.client, flow.token, leased, 1, "intent").status_code == 200
    r = job_result(flow.client, flow.token, leased, status="failed", error_code="plan_stale")
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, deployment)["status"] == "unknown_outcome"


def test_a_palantir_receipt_records_the_installation_for_its_application(
    flow: Flow, owner_db: Database
) -> None:
    approved = _approved(flow)
    leased = lease_kind(flow.client, flow.token, "apply")
    resource = {
        **binding(approved["target_id"], "palantir:x"),
        "key": "installation",
        "resource_type": RESOURCE,
    }
    value = receipt_value(
        flow.tenant, approved["target_id"], approved["artifact_digest"], [resource]
    )
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    with owner_db.session() as o:
        owned = o.execute(
            text(
                "SELECT resource_type, native_id, application_key FROM "
                "shifde.delivery_native_resources WHERE target_id = :g"
            ),
            {"g": approved["target_id"]},
        ).all()
    assert [tuple(r) for r in owned] == [(RESOURCE, "palantir:x", "sales-app")]


def test_the_delivered_palantir_spec_is_the_constant() -> None:
    assert palantir_source.DELIVERY_SPEC.frontend_asset_path == "index.html"
