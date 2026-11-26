"""Fabric plans and results on the server (SP8-5): the kind gate, the `/1` template artifact, the job
context, the revision check at sealing, the apply receipt check (precondition E) and the required
verification checks."""

from __future__ import annotations

import json
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from shifde.adapters.native.base import read_archive
from shifde.adapters.native.contracts import JobResultSubmission
from shifde.adapters.native.fabric import source
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.core.db import Database
from shifde.core.errors import ValidationFailed
from shifde.core.settings import Settings
from shifde.delivery import gateway, plans, service
from shifde.packaging.canonical import canonical
from tests.connections.routes import as_user
from tests.delivery.conftest import ExecutorSide
from tests.delivery.fabric import (
    CHECKS,
    approved_deployment,
    fabric_release,
    fabric_target,
    flow_runner,
    receipt,
    run_due,
    tenant_rows,
    workspace_of,
)
from tests.delivery.releases import release_for_target
from tests.delivery.routes import (
    FABRIC_BINDINGS,
    Flow,
    address,
    bearer,
    delivery_client,
    deployment_row,
    job_result,
    job_row,
    job_url,
    lease_kind,
    lease_plan,
    make_due,
    owner_sql,
    plan_result,
    plan_row,
    proof,
    receipt_value,
    register,
    request_plan,
    snowflake_address,
    snowflake_body,
    sp8_address,
    sp8_body,
    target_body,
)
from tests.tenancy.conftest import app_session

OTHER_KINDS = (
    "databricks.app/v1",
    "snowflake.native-app/v1",
    "palantir.marketplace/v1",
    "docker.compose/v1",
    "fabric.iq/v1",
)
CONTEXT_KEYS = {"workspace", "lakehouse_name", "notebook_name", "entra_tenant_id"}


def _ask(flow: Flow, target: dict[str, Any], release: object, status: int = 201) -> Any:
    h = as_user(flow.client, flow.requester)
    made = request_plan(flow.client, h, flow.slug, target["id"], release, status)
    flow.client.cookies.clear()
    return made


def _refused(flow: Flow, target: dict[str, Any], release: object, code: str) -> dict[str, Any]:
    before = tenant_rows(flow.owner_db, flow.tenant)
    body: dict[str, Any] = _ask(flow, target, release, 409)
    assert body["code"] == code, body
    assert tenant_rows(flow.owner_db, flow.tenant) == before
    return body


def _content(owner_db: Database, artifact_id: object) -> tuple[str, bytes]:
    with owner_db.session() as o:
        row = o.execute(
            text("SELECT target_kind, content FROM shifde.delivery_artifacts WHERE id = :a"),
            {"a": artifact_id},
        ).one()
    return str(row[0]), bytes(row[1])


# ------------------------------------------------------------------ the kind gate
@pytest.mark.parametrize("platform", ["databricks", "snowflake", "palantir", "docker"])
def test_a_fabric_release_is_planned_on_no_other_platform(flow: Flow, platform: str) -> None:
    bodies: dict[str, Any] = {
        "databricks": lambda: target_body(flow.workspace, address()),
        "snowflake": lambda: snowflake_body(flow.workspace, snowflake_address()),
        "palantir": lambda: sp8_body("palantir", flow.workspace, sp8_address("palantir")),
        "docker": lambda: sp8_body("docker", flow.workspace, sp8_address("docker")),
    }
    h = as_user(flow.client, flow.requester)
    target = register(flow.client, h, flow.slug, bodies[platform]())
    flow.client.cookies.clear()
    release = fabric_release(flow.owner_db, target["id"])
    _refused(flow, target, release.id, "target_kind_mismatch")


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_no_other_kind_is_planned_on_a_fabric_target(flow: Flow, kind: str) -> None:
    target = fabric_target(flow)
    release = fabric_release(flow.owner_db, target["id"], kind=kind)
    _refused(flow, target, release.id, "target_kind_mismatch")


def test_a_fabric_plan_needs_a_passing_evaluation(flow: Flow) -> None:
    target = fabric_target(flow)
    release = fabric_release(flow.owner_db, target["id"], evaluation=None)
    _refused(flow, target, release.id, "evaluation_required")


def test_a_fabric_release_with_a_generator_version_is_refused_without_a_write(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    def app(manifest: dict[str, Any]) -> None:
        manifest["compiler_version"] = "shifde.packaging/2"
        manifest["app"] = {"generator_version": "shifde.appgen/1.0"}

    monkeypatch.setattr(
        plans, "template_artifact", lambda *a: pytest.fail("the Databricks template was used")
    )
    target = fabric_target(flow)
    release = fabric_release(flow.owner_db, target["id"], edit=app)
    body = _refused(flow, target, release.id, "generator_version_unsupported")
    assert body["detail"] == "no app generator renders a Fabric release"


# ------------------------------------------------------------------ the artifact
def test_the_fabric_artifact_is_the_template_and_its_release(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        plans, "template_artifact", lambda *a: pytest.fail("the Databricks template was used")
    )
    target = fabric_target(flow)
    release = fabric_release(flow.owner_db, target["id"])
    made = _ask(flow, target, release.id)
    kind, content = _content(flow.owner_db, made["artifact_id"])
    assert kind == "fabric.solution/v1"
    files = read_archive(content)
    assert set(files) == {"lake.json", "main.ipynb", "release.json"}
    templates = resource_files("shifde.adapters.native.fabric").joinpath("templates")
    assert files["lake.json"] == templates.joinpath("lake.json").read_bytes()
    notebook = json.loads(files["main.ipynb"])
    assert notebook["cells"].pop(0) == source.exit_cell(release.digest)
    assert notebook == json.loads(templates.joinpath("main.ipynb").read_bytes())
    assert files["release.json"] == canonical(
        {"manifest": release.manifest, "digest": release.digest}
    )
    # built twice: one digest
    assert plans.fabric_artifact(release.manifest, release.digest) == (
        content,
        made["artifact_digest"],
    )
    again = _ask(flow, target, release.id)
    assert again["artifact_digest"] == made["artifact_digest"] and again["id"] != made["id"]


def test_the_databricks_template_artifact_is_unchanged_by_the_fabric_one(flow: Flow) -> None:
    target = flow.target()
    release = release_for_target(flow.owner_db, target["id"])
    made = _ask(flow, target, release.id)
    _, content = _content(flow.owner_db, made["artifact_id"])
    assert (content, made["artifact_digest"]) == plans.template_artifact(
        release.manifest, release.digest
    )


def test_a_plan_whose_targets_revision_changed_is_not_sealed(flow: Flow) -> None:
    target = fabric_target(flow)
    made = _ask(flow, target, fabric_release(flow.owner_db, target["id"]).id)
    leased = lease_plan(flow.client, flow.token)
    owner_sql(
        flow.owner_db,
        "UPDATE shifde.delivery_targets SET revision = revision + 1 WHERE id = :t",
        t=target["id"],
    )
    assert plan_result(flow.client, flow.token, leased).status_code == 200
    row = plan_row(flow.owner_db, made["id"])
    assert (row["status"], row["status_reason"]) == ("plan_failed", "the target changed")
    assert row["digest"] is None


# ------------------------------------------------------------------ the job context
def _context(flow: Flow, leased: dict[str, Any]) -> dict[str, Any]:
    r = flow.client.get(
        job_url(leased["job"]["id"], "context"), params=proof(leased), headers=bearer(flow.token)
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def test_a_fabric_context_names_the_api_origin_and_the_workspace(flow: Flow) -> None:
    target = fabric_target(flow, url=str(uuid4()).upper())  # stored lowercase
    _ask(flow, target, fabric_release(flow.owner_db, target["id"]).id)
    leased = lease_plan(flow.client, flow.token)
    body = _context(flow, leased)
    assert body["platform_kind"] == "fabric"
    assert body["target"] == {
        "id": target["id"],
        "tenant_id": str(flow.tenant),
        "revision": target["revision"],
        "environment": target["environment"],
        "host": "https://api.fabric.microsoft.com",
        "bindings": {**FABRIC_BINDINGS, "workspace": workspace_of(target)},
    }
    assert set(body["target"]["bindings"]) == CONTEXT_KEYS
    assert workspace_of(target) == workspace_of(target).lower()
    r = flow.client.post(
        job_url(leased["job"]["id"], "credential-grant"),
        json=proof(leased),
        headers=bearer(flow.token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["audience"] == target["address"]
    assert r.json()["credential_ref"] == target["credential_ref"]


def test_another_kinds_context_is_unchanged(flow: Flow) -> None:
    target = flow.target()
    _ask(flow, target, flow.release(target["id"]))
    body = _context(flow, lease_plan(flow.client, flow.token))
    assert body["target"]["host"] == target["address"]
    assert body["target"]["bindings"] == {"warehouse_id": target["bindings"]["warehouse_id"]}


# ------------------------------------------------------------------ the apply receipt (E)
@pytest.fixture
def applying(flow: Flow, tmp_path: Path) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """(target, deployment, the leased apply job) of an approved Fabric plan."""
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, FakeFabricWorkspace())
    release = fabric_release(flow.owner_db, target["id"])
    deployment = approved_deployment(flow, runner, target, release.id)
    return target, deployment, lease_kind(flow.client, flow.token, "apply")


def _value(flow: Flow, target: dict[str, Any], deployment: str, **changes: Any) -> dict[str, Any]:
    plan = plan_row(flow.owner_db, deployment_row(flow.owner_db, deployment)["plan_id"])
    return receipt(flow.tenant, target, plan["artifact_digest"], **changes)


def _post(flow: Flow, leased: dict[str, Any], value: dict[str, Any]) -> Any:
    return job_result(flow.client, flow.token, leased, status="succeeded", value=value)


@pytest.mark.parametrize("env", ["production", "staging"])
@pytest.mark.parametrize("handles", [{"platform": "fake"}, {}, {"platform": "Fabric"}])
def test_outside_development_and_test_only_a_fabric_platform_receipt_is_accepted(
    flow: Flow,
    settings: Settings,
    applying: tuple[dict[str, Any], str, dict[str, Any]],
    env: str,
    handles: dict[str, str],
) -> None:
    """Through the result route of an API whose environment is `env`."""
    target, deployment, leased = applying
    value = _value(flow, target, deployment, operation_handles=handles)
    before = tenant_rows(flow.owner_db, flow.tenant)
    with delivery_client(settings.model_copy(update={"env": env})) as live:
        r = job_result(live, flow.token, leased, status="succeeded", value=value)
        assert r.status_code == 422, r.text
        assert r.json()["errors"][0]["loc"] == ["body", "value", "operation_handles"]
        assert tenant_rows(flow.owner_db, flow.tenant) == before
        value["operation_handles"] = {"platform": "fabric"}
        r = job_result(live, flow.token, leased, status="succeeded", value=value)
        assert r.status_code == 200, r.text
    assert deployment_row(flow.owner_db, deployment)["status"] == "observing"


def test_in_test_a_fake_receipt_is_accepted(
    flow: Flow, applying: tuple[dict[str, Any], str, dict[str, Any]]
) -> None:
    target, deployment, leased = applying
    assert _post(flow, leased, _value(flow, target, deployment)).status_code == 200
    assert deployment_row(flow.owner_db, deployment)["status"] == "observing"


def test_the_service_refuses_a_fake_receipt_unless_told_the_environment(
    flow: Flow,
    executor_a: ExecutorSide,
    app_db: Database,
    applying: tuple[dict[str, Any], str, dict[str, Any]],
) -> None:
    """`submit_result` without an environment refuses, like production."""
    target, deployment, leased = applying
    executor = service.AuthenticatedExecutor(
        flow.tenant, UUID(executor_a.executor_id), "executor", "active", None, None
    )
    body = JobResultSubmission(
        **proof(leased), status="succeeded", value=_value(flow, target, deployment)
    )
    before = tenant_rows(flow.owner_db, flow.tenant)
    job_id = UUID(leased["job"]["id"])
    for env in ({}, {"env": "production"}):
        with app_session(app_db, flow.tenant) as s, pytest.raises(ValidationFailed):
            gateway.submit_result(s, executor, job_id, body, **env)
        assert tenant_rows(flow.owner_db, flow.tenant) == before
    with app_session(app_db, flow.tenant) as s:
        gateway.submit_result(s, executor, job_id, body, env="development")
    assert deployment_row(flow.owner_db, deployment)["status"] == "observing"


@pytest.mark.parametrize("env", ["test", "production"])
@pytest.mark.parametrize("wrong", ["type", "workspace", "prefix"])
def test_a_resource_outside_the_targets_workspace_is_refused_in_any_environment(
    flow: Flow,
    settings: Settings,
    applying: tuple[dict[str, Any], str, dict[str, Any]],
    env: str,
    wrong: str,
) -> None:
    target, deployment, leased = applying
    value = _value(flow, target, deployment, operation_handles={"platform": "fabric"})
    change = {
        "type": {"resource_type": "databricks.app"},
        "workspace": {"native_id": f"fabric:{uuid4()}:{uuid4()}"},
        "prefix": {"native_id": str(uuid4())},
    }[wrong]
    value["resources"][1].update(change)
    before = tenant_rows(flow.owner_db, flow.tenant)
    with delivery_client(settings.model_copy(update={"env": env})) as api:
        r = job_result(api, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 422, r.text
    assert r.json()["errors"][0]["loc"] == ["body", "value", "resources"]
    assert workspace_of(target) not in r.text
    assert tenant_rows(flow.owner_db, flow.tenant) == before


def test_another_kinds_receipt_is_accepted_in_production(flow: Flow, settings: Settings) -> None:
    approved, deployment = flow.deployment()
    leased = lease_kind(flow.client, flow.token, "apply")
    value = receipt_value(flow.tenant, approved["target_id"], approved["artifact_digest"])
    value["operation_handles"]["platform"] = "fake"
    with delivery_client(settings.model_copy(update={"env": "production"})) as live:
        r = job_result(live, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    assert deployment_row(flow.owner_db, deployment)["status"] == "observing"


# ------------------------------------------------------------------ the required checks
def _checks(*names: str, status: str = "passed") -> list[dict[str, str]]:
    return [{"name": n, "status": status} for n in names]


def test_the_fabric_row_requires_its_two_checks_and_no_generated_app() -> None:
    assert service.REQUIRED_BY_PLATFORM["fabric"] == (CHECKS, None)
    assert CHECKS == ("native-items", "runtime-health")


@pytest.mark.parametrize(
    ("checks", "status"),
    [
        (_checks(*CHECKS), "verified"),
        (_checks("native-items"), "failed"),
        (_checks("runtime-health"), "failed"),
        ([*_checks("native-items"), *_checks("runtime-health", status="failed")], "failed"),
        ([*_checks("native-items", status="skipped"), *_checks("runtime-health")], "failed"),
        (_checks("native-runtime", "runtime-warehouse", "app-deep-health"), "failed"),
        ([], "failed"),
    ],
)
@pytest.mark.parametrize("generated", [False, True])
def test_a_fabric_deployment_needs_its_two_checks(
    flow: Flow,
    monkeypatch: pytest.MonkeyPatch,
    applying: tuple[dict[str, Any], str, dict[str, Any]],
    checks: list[dict[str, str]],
    status: str,
    generated: bool,
) -> None:
    """And a plan whose release carries an app generator version is never verified."""
    target, deployment, leased = applying
    assert _post(flow, leased, _value(flow, target, deployment)).status_code == 200
    for kind, value in (
        ("observe", {"state": "succeeded"}),
        ("verify", {"checks": checks}),
    ):
        make_due(flow.owner_db, deployment)
        leased = lease_kind(flow.client, flow.token, kind)
        if kind == "verify" and generated:
            monkeypatch.setattr(service, "app_generator_version", lambda manifest: "x/1.0")
        r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
        assert r.status_code == 200, r.text
    expected = "failed" if generated else status
    assert deployment_row(flow.owner_db, deployment)["status"] == expected


def test_foreign_item_is_the_plans_fixed_reason(flow: Flow) -> None:
    target = fabric_target(flow)
    made = _ask(flow, target, fabric_release(flow.owner_db, target["id"]).id)
    leased = lease_plan(flow.client, flow.token)
    r = plan_result(flow.client, flow.token, leased, status="failed", error_code="foreign_item")
    assert r.status_code == 200, r.text
    row = plan_row(flow.owner_db, made["id"])
    assert (row["status"], row["status_reason"]) == (
        "plan_failed",
        "plan job failed: foreign_item",
    )
    assert job_row(flow.owner_db, leased["job"]["id"])["result"] == {
        "status": "failed",
        "error_code": "foreign_item",
    }


def test_a_verified_deployment_has_the_two_checks_of_the_adapter(
    flow: Flow, tmp_path: Path
) -> None:
    target = fabric_target(flow)
    runner = flow_runner(flow, target, tmp_path, FakeFabricWorkspace())
    release = fabric_release(flow.owner_db, target["id"])
    deployment = approved_deployment(flow, runner, target, release.id)
    run_due(flow, runner, deployment, "apply", "observe", "verify")
    row = deployment_row(flow.owner_db, deployment)
    assert row["status"] == "verified"
    assert [c["name"] for c in row["verification"]["checks"]] == list(CHECKS)
