"""SP5-6: a plan of a `/2` release deploys the app `appgen.render` produces (design 6.6). The three
serving endpoint and model refusals in their order, `release_source_changed` for a changed pin
and for a pin of another organization, `generator_version_unsupported`, an accepted plan whose
artifact is A's and is the render of the same inputs, the plan digest with the generator version,
the unchanged template path, the server's `app-deep-health` rule, and end to end on the native
fake: `verified`, `failed` under `deep_health_bad`, and organization B's executor leasing and
reaching nothing of A's generated-app jobs (design 9.5)."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.native.base import create_archive, read_archive
from shifde.adapters.native.contracts import DatabricksApplicationSpec, JobContext, JobLease
from shifde.adapters.native.databricks.source import scaffold_files
from shifde.appgen.render import GENERATOR_VERSION, render
from shifde.appgen.schemas import Resolved
from shifde.connections.service import ConnectionService
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery import service
from shifde.executor.client import GatewayClient
from shifde.packaging.canonical import canonical, digest
from tests.appgen.rendered import ENDPOINT
from tests.connections.routes import as_user
from tests.delivery.app_releases import (
    AppRelease,
    add_provider,
    app_release_for_target,
    make_app_release,
)
from tests.delivery.conftest import make_executor
from tests.delivery.routes import (
    BOTH_CHECKS,
    Flow,
    address,
    bearer,
    deployment_row,
    job_result,
    job_url,
    lease_kind,
    lease_plan,
    make_due,
    plan_result,
    plan_row,
    plan_value,
    register,
    request_plan,
    target_body,
)
from tests.executor.support import ask_plan, credentials_file, fake_platform, make_runner
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

DEEP_HEALTH = {"name": "app-deep-health", "status": "passed"}


def app_target(flow: Flow, endpoint: str = ENDPOINT) -> dict[str, Any]:  # Any: a TargetOut
    """A Databricks target of A bound to `endpoint`."""
    h = as_user(flow.client, flow.requester)
    bindings = {"warehouse_id": "abc123def456", "serving_endpoint": endpoint}
    made: dict[str, Any] = register(
        flow.client, h, flow.slug, target_body(flow.workspace, address(), bindings=bindings)
    )
    flow.client.cookies.clear()
    return made


def _fake_release(flow: Flow, target: dict[str, Any], **options: Any) -> AppRelease:
    """A `/2` release of A in the target's workspace bound to a `fake` provider of A."""
    provider = add_provider(flow.owner_db, flow.tenant, "fake", None)
    return app_release_for_target(flow.owner_db, target["id"], provider_id=provider, **options)


def _ask(flow: Flow, target: dict[str, Any], release_id: object, status: int = 201) -> Any:
    h = as_user(flow.client, flow.requester)
    body = request_plan(flow.client, h, flow.slug, target["id"], release_id, status=status)
    flow.client.cookies.clear()
    return body


def _writes(owner_db: Database, tenant: UUID) -> tuple[int, int, int]:
    """(artifacts, plans, jobs) of `tenant`, read as the schema owner."""
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_artifacts WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_plans WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t)"
            ),
            {"t": tenant},
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


def _refused(flow: Flow, target: dict[str, Any], release_id: object, code: str) -> Any:
    before = _writes(flow.owner_db, flow.tenant)
    body = _ask(flow, target, release_id, status=409)
    assert body["code"] == code, body
    assert _writes(flow.owner_db, flow.tenant) == before
    return body


# ------------------------------------------------------------------ plan refusals
def test_serving_endpoint_refusals_come_in_order_and_write_nothing(flow: Flow) -> None:
    """One release failing every check: the target decides which refusal comes first."""
    plain, other, bound = flow.target(), app_target(flow, "other-endpoint"), app_target(flow)
    elsewhere = add_provider(
        flow.owner_db,
        flow.tenant,
        "openai_compatible",
        "https://adb-elsewhere.cloud.databricks.com/serving-endpoints",
    )
    rel = app_release_for_target(flow.owner_db, bound["id"], provider_id=elsewhere)
    _refused(flow, plain, rel.id, "serving_endpoint_required")
    _refused(flow, other, rel.id, "serving_endpoint_mismatch")
    body = _refused(flow, bound, rel.id, "model_not_in_target_workspace")
    assert "elsewhere" not in str(body)


def test_model_not_in_target_workspace_for_a_fake_provider_outside_development_and_test(
    flow: Flow, client: TestClient
) -> None:
    target = app_target(flow)
    rel = _fake_release(flow, target)
    runtime = client.app.state.integration  # type: ignore[attr-defined]
    production = runtime.settings.model_copy(update={"env": "production"})
    client.app.state.integration = dataclasses.replace(runtime, settings=production)  # type: ignore[attr-defined]
    _refused(flow, target, rel.id, "model_not_in_target_workspace")


@pytest.mark.parametrize("kind", ["anthropic", "openai_compatible-of-b", "unknown"])
def test_model_not_in_target_workspace_for_another_provider(
    flow: Flow, world: World, kind: str
) -> None:
    target = app_target(flow)
    base = f"{target['address']}/serving-endpoints"
    if kind == "anthropic":
        provider = add_provider(flow.owner_db, flow.tenant, "anthropic", base)
    elif kind == "openai_compatible-of-b":
        provider = add_provider(flow.owner_db, world.b.tenant, "openai_compatible", base)
    else:
        provider = uuid7()
    rel = app_release_for_target(flow.owner_db, target["id"], provider_id=provider)
    _refused(flow, target, rel.id, "model_not_in_target_workspace")


def test_model_not_in_target_workspace_when_the_provider_connection_is_unreadable(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = app_target(flow)
    base = f"{target['address']}/serving-endpoints/{ENDPOINT}/invocations"
    provider = add_provider(flow.owner_db, flow.tenant, "openai_compatible", base)
    rel = app_release_for_target(flow.owner_db, target["id"], provider_id=provider)
    asked: list[UUID] = []

    def unreadable(_: ConnectionService, connection_id: UUID) -> bool:
        asked.append(connection_id)
        return False

    monkeypatch.setattr(ConnectionService, "readable", unreadable)
    _refused(flow, target, rel.id, "model_not_in_target_workspace")
    assert len(asked) == 1


def test_release_source_changed_for_a_changed_spec_hash(flow: Flow) -> None:
    target = app_target(flow)

    def tamper(manifest: dict[str, Any]) -> None:
        manifest["pins"][0]["spec_hash"] = "0" * 64

    _refused(flow, target, _fake_release(flow, target, edit=tamper).id, "release_source_changed")


def test_release_source_changed_for_a_pin_of_another_organization(
    flow: Flow, world: World, owner_db: Database
) -> None:
    with owner_db.session() as o:
        b_workspace = o.execute(
            text("SELECT id FROM shifde.workspaces WHERE tenant_id = :t ORDER BY id LIMIT 1"),
            {"t": world.b.tenant},
        ).scalar_one()
    b = make_app_release(
        owner_db,
        world.b.tenant,
        b_workspace,
        provider_id=add_provider(owner_db, world.b.tenant, "fake", None),
    )
    b_tool = next(iter(b.tools.values()))
    target = app_target(flow)

    def b_pin(manifest: dict[str, Any]) -> None:
        tool = next(p for p in manifest["pins"] if p["role"] == "tool")
        tool.update(version_id=str(b_tool[0]), spec_hash=b_tool[1])

    body = _refused(
        flow, target, _fake_release(flow, target, edit=b_pin).id, "release_source_changed"
    )
    assert str(b_tool[0]) not in str(body) and b_tool[1] not in str(body)


def test_generator_version_unsupported_writes_nothing(flow: Flow) -> None:
    target = app_target(flow)

    def other_version(manifest: dict[str, Any]) -> None:
        manifest["app"]["generator_version"] = "shifde.appgen/9.0"

    rel = _fake_release(flow, target, edit=other_version)
    _refused(flow, target, rel.id, "generator_version_unsupported")


# ------------------------------------------------------------------ accepted plans
@pytest.mark.parametrize("kind", ["fake", "openai_compatible"])
def test_an_accepted_generated_plan_stores_the_render_as_as_artifact(
    flow: Flow, owner_db: Database, kind: str
) -> None:
    target = app_target(flow)
    if kind == "fake":
        rel = _fake_release(flow, target)
    else:
        base = f"{target['address']}/serving-endpoints/{ENDPOINT}/invocations"
        provider = add_provider(owner_db, flow.tenant, kind, base)
        rel = app_release_for_target(owner_db, target["id"], provider_id=provider)
    made = _ask(flow, target, rel.id)
    with owner_db.session() as o:
        artifact = (
            o.execute(
                text("SELECT * FROM shifde.delivery_artifacts WHERE id = :a"),
                {"a": made["artifact_id"]},
            )
            .mappings()
            .one()
        )
    expected = render(
        rel.release.manifest, rel.release.digest, Resolved.model_validate(rel.resolved())
    )
    content = bytes(artifact["content"])
    assert artifact["tenant_id"] == flow.tenant and artifact["target_kind"] == "databricks.app/v1"
    assert read_archive(content) == expected
    assert artifact["digest"] == made["artifact_digest"]
    assert artifact["digest"] == hashlib.sha256(create_archive(expected)).hexdigest()
    assert b"serving-endpoints" not in content  # the provider's base_url is never stored

    # sealing: the plan digest input carries the generator version
    leased = lease_plan(flow.client, flow.token)
    assert plan_result(flow.client, flow.token, leased).status_code == 200
    row = plan_row(owner_db, made["id"])
    value = plan_value()
    common = {
        "artifact_digest": row["artifact_digest"],
        "target_revision": row["target_revision"],
        "snapshot_fingerprint": value["snapshot"]["fingerprint"],
        "steps": row["steps"],
        "adapter_version": value["adapter_version"],
        "toolchain_digest": value["toolchain_digest"],
        "release_digest": rel.release.digest,
    }
    assert row["status"] == "sealed"
    assert row["digest"] == service.plan_digest(**common, generator_version=GENERATOR_VERSION)
    assert row["digest"] != service.plan_digest(**common)


def test_a_template_plan_keeps_its_artifact_and_plan_digests(flow: Flow) -> None:
    """A `/1` release on a target naming an endpoint: today's template archive and the digest of
    exactly the seven SP3 keys."""
    target = app_target(flow)
    release_id = flow.release(target["id"])
    sealed = flow.sealed(target["id"], release_id=release_id)
    row = plan_row(flow.owner_db, sealed["id"])
    with flow.owner_db.session() as o:
        manifest, release_digest = o.execute(
            text("SELECT manifest, digest FROM shifde.releases WHERE id = :r"), {"r": release_id}
        ).one()
    files = scaffold_files(DatabricksApplicationSpec())
    files["release.json"] = canonical({"manifest": manifest, "digest": release_digest})
    assert sealed["artifact_digest"] == hashlib.sha256(create_archive(files)).hexdigest()
    value = plan_value()
    assert sealed["digest"] == digest(
        {
            "artifact_digest": sealed["artifact_digest"],
            "target_revision": row["target_revision"],
            "snapshot_fingerprint": value["snapshot"]["fingerprint"],
            "steps": row["steps"],
            "adapter_version": value["adapter_version"],
            "toolchain_digest": value["toolchain_digest"],
            "release_digest": release_digest,
        }
    )


def test_plan_digest_adds_the_generator_version_only_when_given() -> None:
    common: dict[str, Any] = {
        "artifact_digest": "a" * 64,
        "target_revision": 1,
        "snapshot_fingerprint": "b" * 64,
        "steps": [],
        "adapter_version": "0.1.0",
        "toolchain_digest": "c" * 64,
        "release_digest": "d" * 64,
    }
    assert service.plan_digest(**common) == digest(common)
    with_version = service.plan_digest(**common, generator_version=GENERATOR_VERSION)
    assert with_version == digest({**common, "generator_version": GENERATOR_VERSION})
    assert with_version != service.plan_digest(**common)


# ------------------------------------------------------------------ server verification rule
def _verifying(
    flow: Flow, owner_db: Database, target: dict[str, Any], release_id: object
) -> tuple[str, Any]:  # Any: the verify job's lease
    """A deployment of `release_id` on `target` with its verify job leased."""
    sealed = flow.sealed(target["id"], release_id=release_id)
    approved = flow.approve(sealed)
    flow.apply(approved)
    deployment = approved["deployment_id"]
    make_due(owner_db, deployment)
    leased = lease_kind(flow.client, flow.token, "observe")
    r = job_result(
        flow.client, flow.token, leased, status="succeeded", value={"state": "succeeded"}
    )
    assert r.status_code == 200, r.text
    make_due(owner_db, deployment)
    return deployment, lease_kind(flow.client, flow.token, "verify")


@pytest.mark.parametrize(
    ("checks", "status"),
    [
        (BOTH_CHECKS, "failed"),
        ([*BOTH_CHECKS, {"name": "app-deep-health", "status": "failed"}], "failed"),
        ([*BOTH_CHECKS, DEEP_HEALTH], "verified"),
    ],
    ids=["omitted", "failed", "passed"],
)
def test_a_generated_deployment_needs_app_deep_health(
    flow: Flow, owner_db: Database, checks: list[dict[str, str]], status: str
) -> None:
    target = app_target(flow)
    deployment, leased = _verifying(flow, owner_db, target, _fake_release(flow, target).id)
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": checks})
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, deployment)["status"] == status


@pytest.mark.parametrize("unreadable", ["plan", "release"])
def test_a_verification_whose_plan_or_release_cannot_be_read_fails_closed(
    flow: Flow, owner_db: Database, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    target = app_target(flow)
    deployment, leased = _verifying(flow, owner_db, target, _fake_release(flow, target).id)
    if unreadable == "plan":
        monkeypatch.setattr(service.repository, "get_plan", lambda *a, **k: None)
    else:
        monkeypatch.setattr(service, "release_record", lambda *a, **k: None)
    checks = [*BOTH_CHECKS, DEEP_HEALTH]
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": checks})
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, deployment)["status"] == "failed"


# ------------------------------------------------------------------ end to end (native fake)
def _rows(owner_db: Database, tenant: UUID) -> dict[str, list[dict[str, Any]]]:
    """A's artifacts, jobs, plans and deployments, read as the schema owner."""
    with owner_db.session() as o:
        return {
            table: [
                dict(r)
                for r in o.execute(
                    # the table names are the literals below
                    text(f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY id"),  # nosec B608
                    {"t": tenant},
                ).mappings()
            ]
            for table in (
                "delivery_artifacts",
                "delivery_jobs",
                "delivery_plans",
                "delivery_deployments",
            )
        }


def _b_reaches_nothing(client: TestClient, owner_db: Database, tenant: UUID, b: Any) -> str:
    """While one job of A is due: B's runner leases nothing, B's artifact and context calls on
    it get the unknown job's 404, and none of A's rows change. The due job's kind."""
    before = _rows(owner_db, tenant)
    (due,) = [j for j in before["delivery_jobs"] if j["status"] == "queued"]
    assert not b.runner.run_once()
    forged = {"lease_id": str(uuid7()), "fence": 1}
    for action in ("artifact", "context"):
        missing = client.get(job_url(uuid7(), action), params=forged, headers=bearer(b.token))
        r = client.get(job_url(due["id"], action), params=forged, headers=bearer(b.token))
        assert r.status_code == missing.status_code == 404, r.text
        assert r.json()["code"] == missing.json()["code"]
    assert _rows(owner_db, tenant) == before
    return str(due["kind"])


@dataclasses.dataclass
class _B:
    token: str
    runner: Any  # Any: a Runner


@pytest.mark.parametrize("fault", [False, True], ids=["healthy", "deep_health_bad"])
def test_a_generated_app_end_to_end_on_the_native_fake(
    client: TestClient,
    flow: Flow,
    world: World,
    people: People,
    owner_db: Database,
    tmp_path: Path,
    fault: bool,
) -> None:
    fake = fake_platform()
    fake.deep_health_bad = fault
    target = app_target(flow)
    rel = _fake_release(flow, target)
    runner_a = make_runner(
        client,
        flow.token,
        credentials_file(tmp_path, target["address"], tenant=flow.tenant, name="a.json"),
        fake,
    )
    side = make_executor(client, world.b, people.owner_b)
    b_host = "https://adb-organization-b.cloud.databricks.com"
    b = _B(
        side.token,
        make_runner(
            client,
            side.token,
            credentials_file(tmp_path, b_host, tenant=side.tenant, name="b.json"),
            fake,
        ),
    )

    plan = ask_plan(flow, target["id"], release_id=rel.id)
    kinds = [_b_reaches_nothing(client, owner_db, flow.tenant, b)]
    assert runner_a.run_once()
    sealed = flow.get_plan(plan["id"])
    assert sealed["status"] == "sealed", sealed
    deployment = flow.approve(sealed)["deployment_id"]
    kinds.append(_b_reaches_nothing(client, owner_db, flow.tenant, b))
    assert runner_a.run_once()  # apply
    for _ in ("observe", "verify"):
        make_due(owner_db, deployment)
        kinds.append(_b_reaches_nothing(client, owner_db, flow.tenant, b))
        assert runner_a.run_once()
    assert kinds == ["plan", "apply", "observe", "verify"]
    assert fake.actions and all(root.startswith("/Workspace/Users/") for _, _, root in fake.actions)

    body = flow.get(deployment)
    checks = {c["name"]: c for c in body["verification"]["checks"]}
    assert set(checks) == {"native-runtime", "runtime-warehouse", "app-deep-health"}
    if not fault:
        assert body["status"] == "verified", body
        assert all(c["status"] == "passed" for c in checks.values())
    else:
        assert body["status"] == "failed", body
        assert checks["app-deep-health"]["status"] == "failed"
        assert "the app cannot read" in checks["app-deep-health"]["evidence"]


def test_a_generated_plan_without_a_release_digest_fails_verify_in_the_runner(
    client: TestClient,
    flow: Flow,
    owner_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_platform()
    target = app_target(flow)
    rel = _fake_release(flow, target)
    runner = make_runner(
        client,
        flow.token,
        credentials_file(tmp_path, target["address"], tenant=flow.tenant, name="a.json"),
        fake,
    )
    plan = ask_plan(flow, target["id"], release_id=rel.id)
    assert runner.run_once()
    deployment = flow.approve(flow.get_plan(plan["id"]))["deployment_id"]
    assert runner.run_once()  # apply
    make_due(owner_db, deployment)
    assert runner.run_once()  # observe
    context = GatewayClient.context

    def no_release_digest(self: GatewayClient, lease: JobLease) -> JobContext:
        ctx = context(self, lease)
        assert ctx.plan is not None and ctx.plan.generator_version == GENERATOR_VERSION
        plan = ctx.plan.model_copy(update={"release_digest": None})
        return ctx.model_copy(update={"plan": plan})

    monkeypatch.setattr(GatewayClient, "context", no_release_digest)
    make_due(owner_db, deployment)
    assert runner.run_once()  # verify
    body = flow.get(deployment)
    assert body["status"] == "failed", body
    assert body["verification"] is None
