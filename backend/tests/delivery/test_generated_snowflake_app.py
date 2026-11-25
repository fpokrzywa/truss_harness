"""SP6-6: a plan on a Snowflake target deploys the Native App `appgen.render` produces (SP6 design
6.4). The kind, Cortex model and provider refusals in their order, the accepted plan's artifact and
digests, the server's Snowflake verify mapping, and end to end on the native fake: a first deploy
and a patch-1 upgrade `verified` by the staged deep health, its faults `failed`, the patch limit,
and organization B's executor leasing and reaching nothing of A's Snowflake jobs (section 8)."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.native.base import create_archive, read_archive
from shifde.adapters.native.snowflake.adapter import STEPS
from shifde.adapters.native.snowflake.fake import FakeSnowflakeNativePlatform
from shifde.appgen.render import Resolved, render
from shifde.connections.service import ConnectionService
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery import service
from shifde.executor.runner import Runner
from tests.appgen.rendered import CORTEX_MODEL, SNOWFLAKE_KIND
from tests.connections.routes import as_user
from tests.delivery.app_releases import (
    KINDS,
    AppRelease,
    add_provider,
    app_release_for_target,
    make_app_release,
)
from tests.delivery.conftest import make_executor
from tests.delivery.releases import release_for_target
from tests.delivery.routes import (
    BOTH_CHECKS,
    Flow,
    deployment_row,
    job_result,
    lease_kind,
    lease_plan,
    make_due,
    plan_result,
    plan_row,
    plan_value,
    plans,
)
from tests.delivery.snowflake import app_release, app_target, approve, snowflake_plan
from tests.delivery.test_generated_app import (
    _B,
    _ask,
    _b_reaches_nothing,
    _refused,
    _rows,
    _verifying,
    _writes,
)
from tests.delivery.test_generated_app import app_target as databricks_target
from tests.executor.support import (
    ask_plan,
    credentials_file,
    fake_platform,
    make_runner,
    snowflake_credentials_file,
    snowflake_fake,
    snowflake_key_pem,
)
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

GENERATOR_VERSION = "shifde.appgen.snowflake/1.0"
PEM = snowflake_key_pem()
CHECKS = ("native-version", "runtime-health", "app-deep-health")
ORDERS = "DEMO_DB.PUBLIC.ORDERS"
Manifest = dict[str, Any]  # Any: a release manifest


def _b_workspace(owner_db: Database, tenant: UUID) -> UUID:
    with owner_db.session() as o:
        found: UUID = o.execute(
            text("SELECT id FROM shifde.workspaces WHERE tenant_id = :t ORDER BY id LIMIT 1"),
            {"t": tenant},
        ).scalar_one()
    return found


# ------------------------------------------------------------------ the kind gate
def test_a_release_is_planned_only_on_a_target_of_its_kind(flow: Flow) -> None:
    databricks, snowflake = databricks_target(flow), app_target(flow)
    template = release_for_target(flow.owner_db, snowflake["id"])
    _refused(flow, snowflake, template.id, "target_kind_mismatch")
    provider = add_provider(flow.owner_db, flow.tenant, "fake", None)
    generated = app_release_for_target(flow.owner_db, snowflake["id"], provider_id=provider)
    body = _refused(flow, snowflake, generated.id, "target_kind_mismatch")
    assert body["detail"] == "The release is for another kind of target."
    _refused(flow, databricks, app_release(flow, databricks).id, "target_kind_mismatch")


@pytest.mark.parametrize("target_kind", [None, "palantir.app/v1", ["snowflake.native-app/v1"]])
def test_a_missing_or_unknown_target_kind_is_refused(flow: Flow, target_kind: Any) -> None:
    target = app_target(flow)

    def edit(manifest: Manifest) -> None:
        manifest["target_kind"] = target_kind

    _refused(flow, target, app_release(flow, target, edit=edit).id, "target_kind_mismatch")


def test_the_earlier_checks_come_before_the_kind(flow: Flow, world: World) -> None:
    target = app_target(flow)
    unevaluated = release_for_target(flow.owner_db, target["id"], evaluation=None)
    _refused(flow, target, unevaluated.id, "evaluation_required")
    with flow.owner_db.session() as o:
        workspace = uuid7()
        o.execute(
            text(
                "INSERT INTO shifde.workspaces (id, tenant_id, slug, name) VALUES (:w, :t, :s, :s)"
            ),
            {"w": workspace, "t": flow.tenant, "s": f"w-{workspace.hex}"},
        )
        o.commit()
    provider = add_provider(flow.owner_db, flow.tenant, "fake", None)
    elsewhere = make_app_release(
        flow.owner_db, flow.tenant, workspace, provider_id=provider, kind=SNOWFLAKE_KIND
    )
    _refused(flow, target, elsewhere.id, "release_target_mismatch")


# ------------------------------------------------------------------ the Cortex model gate
def _no_binding(manifest: Manifest) -> None:
    manifest["bindings"]["model_bindings"] = {}


def _two_bindings(manifest: Manifest) -> None:
    bindings = manifest["bindings"]["model_bindings"]
    bindings["second"] = dict(bindings["default"])


def _no_model(manifest: Manifest) -> None:
    del manifest["bindings"]["model_bindings"]["default"]["model"]


def _other_model(manifest: Manifest) -> None:
    manifest["bindings"]["model_bindings"]["default"]["model"] = "llama3.1-70b"


@pytest.mark.parametrize("edit", [_no_binding, _two_bindings, _no_model, _other_model])
def test_cortex_model_mismatch(flow: Flow, edit: Callable[[Manifest], None]) -> None:
    target = app_target(flow)
    _refused(flow, target, app_release(flow, target, edit=edit).id, "cortex_model_mismatch")


def test_the_cortex_model_comes_before_the_provider(flow: Flow) -> None:
    target = app_target(flow)
    release = app_release(flow, target, provider_id=uuid7(), edit=_other_model)
    _refused(flow, target, release.id, "cortex_model_mismatch")


# ------------------------------------------------------------------ the provider gate
def _origin(address: str, case: str) -> str:
    host = address.removeprefix("https://")
    return {
        "other-host": "https://elsewhere-acct.snowflakecomputing.com",
        "privatelink": f"https://{host.removesuffix('.snowflakecomputing.com')}"
        ".privatelink.snowflakecomputing.com",
        "suffixed": f"https://{host}.elsewhere-acct.example",
        "http": f"http://{host}",
        "user-info": f"https://elsewhere-user@{host}",
        "port": f"https://{host}:8443",
    }[case]


@pytest.mark.parametrize(
    "case", ["other-host", "privatelink", "suffixed", "http", "user-info", "port"]
)
def test_a_provider_on_another_origin_is_refused(flow: Flow, case: str) -> None:
    target = app_target(flow)
    base = f"{_origin(target['address'], case)}/api/v2/cortex/v1"
    provider = add_provider(flow.owner_db, flow.tenant, "openai_compatible", base)
    release = app_release(flow, target, provider_id=provider)
    body = _refused(flow, target, release.id, "model_not_in_target_account")
    for hidden in ("elsewhere", str(provider), "snowflakecomputing", "cortex/v1"):
        assert hidden not in str(body)


@pytest.mark.parametrize(
    "case", ["anthropic", "no-base-url", "unknown", "not-a-uuid", "of-b-fake", "of-b"]
)
def test_another_provider_is_refused(flow: Flow, world: World, case: str) -> None:
    target = app_target(flow)
    base = f"{target['address']}/api/v2/cortex/v1"
    provider = uuid7()
    if case == "anthropic":
        provider = add_provider(flow.owner_db, flow.tenant, "anthropic", base)
    elif case == "no-base-url":
        provider = add_provider(flow.owner_db, flow.tenant, "openai_compatible", None)
    elif case.startswith("of-b"):
        kind = "fake" if case == "of-b-fake" else "openai_compatible"
        provider = add_provider(flow.owner_db, world.b.tenant, kind, base)

    def edit(manifest: Manifest) -> None:
        if case == "not-a-uuid":
            manifest["bindings"]["model_bindings"]["default"]["provider_id"] = "provider"

    release = app_release(flow, target, provider_id=provider, edit=edit)
    body = _refused(flow, target, release.id, "model_not_in_target_account")
    assert str(provider) not in str(body) and target["address"] not in str(body)


def test_an_unreadable_provider_connection_is_refused(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = app_target(flow)
    base = f"{target['address']}/api/v2/cortex/v1"
    provider = add_provider(flow.owner_db, flow.tenant, "openai_compatible", base)
    release = app_release(flow, target, provider_id=provider)
    asked: list[UUID] = []

    def unreadable(_: ConnectionService, connection_id: UUID) -> bool:
        asked.append(connection_id)
        return False

    monkeypatch.setattr(ConnectionService, "readable", unreadable)
    _refused(flow, target, release.id, "model_not_in_target_account")
    assert len(asked) == 1


def test_a_fake_provider_is_refused_outside_development_and_test(
    flow: Flow, client: TestClient
) -> None:
    target = app_target(flow)
    release = app_release(flow, target)
    runtime = client.app.state.integration  # type: ignore[attr-defined]
    production = runtime.settings.model_copy(update={"env": "production"})
    client.app.state.integration = dataclasses.replace(runtime, settings=production)  # type: ignore[attr-defined]
    _refused(flow, target, release.id, "model_not_in_target_account")


# ------------------------------------------------------------------ source and render refusals
def test_release_source_changed_and_render_refusals_write_nothing(flow: Flow) -> None:
    target = app_target(flow)

    def tampered(manifest: Manifest) -> None:
        manifest["pins"][0]["spec_hash"] = "0" * 64

    def other_version(manifest: Manifest) -> None:
        manifest["app"]["generator_version"] = "shifde.appgen.snowflake/9.0"

    def template(manifest: Manifest) -> None:
        manifest["compiler_version"] = "shifde.packaging/1"

    def two_part(manifest: Manifest) -> None:
        manifest["data_reads"][0]["tables"].append("public.orders")

    for edit, code in (
        (tampered, "release_source_changed"),
        (other_version, "generator_version_unsupported"),
        (template, "generator_version_unsupported"),
        (two_part, "release_not_renderable"),
    ):
        _refused(flow, target, app_release(flow, target, edit=edit).id, code)


def test_a_private_tool_is_not_named_by_release_not_renderable(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool the requester cannot see, its SQL using a `:name` it does not declare."""
    made = KINDS[SNOWFLAKE_KIND]
    draft = copy.deepcopy(made.draft)
    tool = draft["tools"][0]
    tool["sql"] += " limit :secret_param"
    monkeypatch.setitem(KINDS, SNOWFLAKE_KIND, dataclasses.replace(made, draft=draft))
    target = app_target(flow)
    release = app_release(flow, target)
    with flow.owner_db.session() as o:
        owner = o.execute(
            text(
                "UPDATE shifde.artifacts SET visibility = 'private' WHERE tenant_id = :t AND id = "
                "(SELECT artifact_id FROM shifde.artifact_versions WHERE id = :v) "
                "RETURNING owner_principal_id"
            ),
            {"t": flow.tenant, "v": release.tools[tool["name"]][0]},
        ).scalar_one()
        requester = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": flow.requester}
        ).scalar_one()
        o.commit()
    assert owner != requester
    body = _refused(flow, target, release.id, "release_not_renderable")
    assert body["detail"] == "The release cannot be rendered as a Native App."
    answer = str(body).lower()
    parameters = [p["name"] for p in tool["parameters"]]
    for value in (tool["name"], "secret_param", *parameters, tool["sql"], "select", ORDERS):
        assert value.lower() not in answer, value


def test_a_pin_of_another_organization_is_release_source_changed(
    flow: Flow, world: World, owner_db: Database
) -> None:
    b = make_app_release(
        owner_db,
        world.b.tenant,
        _b_workspace(owner_db, world.b.tenant),
        provider_id=add_provider(owner_db, world.b.tenant, "fake", None),
        kind=SNOWFLAKE_KIND,
    )
    b_tool = next(iter(b.tools.values()))
    b_rows = _rows(owner_db, world.b.tenant)
    target = app_target(flow)

    def b_pin(manifest: Manifest) -> None:
        tool = next(p for p in manifest["pins"] if p["role"] == "tool")
        tool.update(version_id=str(b_tool[0]), spec_hash=b_tool[1])

    release = app_release(flow, target, edit=b_pin)
    body = _refused(flow, target, release.id, "release_source_changed")
    assert str(b_tool[0]) not in str(body) and b_tool[1] not in str(body)
    assert _rows(owner_db, world.b.tenant) == b_rows


# ------------------------------------------------------------------ accepted plans
def _artifact(owner_db: Database, artifact_id: object) -> Any:  # Any: a RowMapping
    with owner_db.session() as o:
        return (
            o.execute(
                text("SELECT * FROM shifde.delivery_artifacts WHERE id = :a"), {"a": artifact_id}
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize("kind", ["fake", "openai_compatible"])
def test_an_accepted_plan_stores_the_render_as_as_artifact(
    flow: Flow, owner_db: Database, kind: str
) -> None:
    target = app_target(flow)
    release: AppRelease
    if kind == "fake":
        release = app_release(flow, target)
    else:
        base = f"{target['address']}/api/v2/cortex/v1"
        provider = add_provider(owner_db, flow.tenant, kind, base)
        release = app_release(flow, target, provider_id=provider)
    made = _ask(flow, target, release.id)
    artifact = _artifact(owner_db, made["artifact_id"])
    expected = render(
        release.release.manifest,
        release.release.digest,
        Resolved.model_validate(release.resolved()),
    )
    content = bytes(artifact["content"])
    assert artifact["tenant_id"] == flow.tenant and artifact["target_kind"] == SNOWFLAKE_KIND
    assert read_archive(content) == expected and content == create_archive(expected)
    assert artifact["digest"] == made["artifact_digest"] == hashlib.sha256(content).hexdigest()
    plain = b"\n".join(expected.values())
    for leak in (b"base_url", b"cortex/v1", b".snowflakecomputing.com", b"-----BEGIN"):
        assert leak not in plain and leak not in content

    # sealing: the plan digest covers the Snowflake generator version
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
        "release_digest": release.release.digest,
    }
    assert row["status"] == "sealed"
    assert row["digest"] == service.plan_digest(**common, generator_version=GENERATOR_VERSION)
    assert row["digest"] != service.plan_digest(**common)

    # the audit row carries the generator version, and nothing written carries the base_url
    with owner_db.session() as o:
        details = o.execute(
            text(
                "SELECT details FROM shifde.audit_events WHERE tenant_id = :t "
                "AND action = 'delivery.plan.requested'"
            ),
            {"t": flow.tenant},
        ).scalar_one()
    assert details["generator_version"] == GENERATOR_VERSION
    for written in (details, dict(row), made):
        assert "cortex/v1" not in str(written)

    # the same release again: the same artifact digest
    again = _ask(flow, target, release.id)
    assert again["artifact_digest"] == made["artifact_digest"] and again["id"] != made["id"]


# ------------------------------------------------------------------ server verification rule
def _checks(*names: str, **failed: str) -> list[dict[str, str]]:
    return [{"name": n, "status": failed.get(n.replace("-", "_"), "passed")} for n in names]


@pytest.mark.parametrize(
    ("checks", "status"),
    [
        (_checks(*CHECKS), "verified"),
        (_checks("runtime-health", "app-deep-health"), "failed"),
        (_checks("native-version", "app-deep-health"), "failed"),
        (_checks("native-version", "runtime-health"), "failed"),
        (_checks(*CHECKS, native_version="failed"), "failed"),
        (_checks(*CHECKS, runtime_health="skipped"), "failed"),
        (_checks(*CHECKS, app_deep_health="failed"), "failed"),
        ([*BOTH_CHECKS, *_checks("app-deep-health")], "failed"),
        ([], "failed"),
    ],
)
def test_a_snowflake_deployment_needs_its_three_checks(
    flow: Flow, owner_db: Database, checks: list[dict[str, str]], status: str
) -> None:
    target = app_target(flow)
    deployment, leased = _verifying(flow, owner_db, target, app_release(flow, target).id)
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": checks})
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, deployment)["status"] == status


def test_a_snowflake_report_never_verifies_a_databricks_deployment(
    flow: Flow, owner_db: Database
) -> None:
    target = databricks_target(flow)
    provider = add_provider(owner_db, flow.tenant, "fake", None)
    release = app_release_for_target(owner_db, target["id"], provider_id=provider)
    deployment, leased = _verifying(flow, owner_db, target, release.id)
    value = {"checks": _checks(*CHECKS)}
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, deployment)["status"] == "failed"


@pytest.mark.parametrize("unreadable", ["target", "plan", "release", "not-generated"])
def test_a_verification_that_cannot_be_decided_fails_closed(
    flow: Flow, owner_db: Database, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    target = app_target(flow)
    deployment, leased = _verifying(flow, owner_db, target, app_release(flow, target).id)
    if unreadable == "target":
        monkeypatch.setattr(service.repository, "get_target", lambda *a, **k: None)
    elif unreadable == "plan":
        monkeypatch.setattr(service.repository, "get_plan", lambda *a, **k: None)
    elif unreadable == "release":
        monkeypatch.setattr(service, "release_record", lambda *a, **k: None)
    else:
        monkeypatch.setattr(service, "app_generator_version", lambda manifest: None)
    value = {"checks": [*BOTH_CHECKS, *_checks(*CHECKS)]}
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, deployment)["status"] == "failed"


def test_a_snowflake_plan_without_a_release_never_verifies(
    flow: Flow, owner_db: Database, app_db: Database, tmp_path: Path
) -> None:
    """A hand-written plan of a test-only artifact: every check posted as passed is `failed`."""
    target = app_target(flow)
    runner = _runner(flow, target, tmp_path, snowflake_fake(target["address"], PEM))
    plan = snowflake_plan(app_db, flow.tenant, target["id"])
    assert runner.run_once()
    deployment = str(approve(app_db, flow.tenant, plan.id))
    _run(flow, runner, deployment, "apply", "observe")
    make_due(owner_db, deployment)
    leased = lease_kind(flow.client, flow.token, "verify")
    value = {"checks": [*BOTH_CHECKS, *_checks(*CHECKS)]}
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    assert deployment_row(owner_db, deployment)["status"] == "failed"


# ------------------------------------------------------------------ end to end (native fake)
def _runner(
    flow: Flow, target: dict[str, Any], directory: Path, fake: FakeSnowflakeNativePlatform
) -> Runner:
    creds = snowflake_credentials_file(directory, target["address"], PEM, tenant=flow.tenant)
    return make_runner(flow.client, flow.token, creds, fake_platform(), fake)


def _run(flow: Flow, runner: Runner, deployment: str, *kinds: str) -> None:
    for _ in kinds:
        make_due(flow.owner_db, deployment)
        assert runner.run_once()


def _deploy(flow: Flow, runner: Runner, target: dict[str, Any], release: AppRelease) -> Any:
    """Request, plan, second-person approval, apply, observe and verify; the deployment body."""
    plan = ask_plan(flow, target["id"], release_id=release.id)
    assert runner.run_once()
    sealed = flow.get_plan(plan["id"])
    assert sealed["status"] == "sealed", sealed
    deployment = flow.approve(sealed)["deployment_id"]
    _run(flow, runner, deployment, "apply", "observe", "verify")
    return flow.get(deployment)


def _native_ids(owner_db: Database, tenant: UUID) -> list[tuple[str, str]]:
    with owner_db.session() as o:
        return sorted(
            (str(r[0]), str(r[1]))
            for r in o.execute(
                text(
                    "SELECT DISTINCT resource_type, native_id "
                    "FROM shifde.delivery_native_resources WHERE tenant_id = :t"
                ),
                {"t": tenant},
            )
        )


def test_a_first_deploy_is_verified_and_bs_executor_reaches_nothing(
    client: TestClient,
    flow: Flow,
    world: World,
    people: People,
    owner_db: Database,
    tmp_path: Path,
) -> None:
    target = app_target(flow)
    release = app_release(flow, target)
    fake = snowflake_fake(target["address"], PEM)
    runner_a = _runner(flow, target, tmp_path, fake)
    side = make_executor(client, world.b, people.owner_b)
    b_creds = credentials_file(
        tmp_path, "https://adb-organization-b.cloud.databricks.com", tenant=side.tenant, name="b"
    )
    b = _B(side.token, make_runner(client, side.token, b_creds, fake_platform(), fake))

    plan = ask_plan(flow, target["id"], release_id=release.id)
    kinds = [_b_reaches_nothing(client, owner_db, flow.tenant, b)]
    assert runner_a.run_once()
    sealed = flow.get_plan(plan["id"])
    assert sealed["status"] == "sealed", sealed
    h = as_user(client, flow.requester)
    own = client.post(
        f"{plans(flow.slug)}/{plan['id']}/approve",
        json={"plan_digest": sealed["digest"]},
        headers=h,
    )
    client.cookies.clear()
    assert (own.status_code, own.json()["code"]) == (403, "self_approval_forbidden")
    deployment = flow.approve(sealed)["deployment_id"]
    kinds.append(_b_reaches_nothing(client, owner_db, flow.tenant, b))
    assert runner_a.run_once()  # apply
    for _ in ("observe", "verify"):
        make_due(owner_db, deployment)
        kinds.append(_b_reaches_nothing(client, owner_db, flow.tenant, b))
        assert runner_a.run_once()
    assert kinds == ["plan", "apply", "observe", "verify"]

    body = flow.get(deployment)
    assert {s["step_key"]: s["state"] for s in body["journal"]} == dict.fromkeys(STEPS, "complete")
    writes = "\n".join(fake.writes())
    for written in (" ADD VERSION V1 ", "CREATE APPLICATION ", " PATCH 0"):
        assert written in writes
    assert {c["name"]: c["status"] for c in body["verification"]["checks"]} == dict.fromkeys(
        CHECKS, "passed"
    )
    assert body["status"] == "verified", body
    # the server asked for the staged handler's answer, which names the sealed release
    (stage,) = fake.state(target["address"]).stages.values()
    assert f"{plan['artifact_digest']}/ui/deep_health.py" in stage["files"]
    assert release.release.digest != plan["artifact_digest"]


@pytest.mark.parametrize(
    ("fault", "evidence"),
    [
        (f"table_denied:{ORDERS}", (f"table:{ORDERS}", "the app cannot read")),
        (
            "cortex_denied",
            ("serving_endpoint", f"the app cannot use Cortex model {CORTEX_MODEL}"),
        ),
        (
            "deep_health_bad",
            ("serving_endpoint", f"the app cannot use Cortex model {CORTEX_MODEL}"),
        ),
    ],
)
def test_a_deep_health_fault_fails_the_deployment(
    flow: Flow, tmp_path: Path, fault: str, evidence: tuple[str, ...]
) -> None:
    target = app_target(flow)
    fake = snowflake_fake(target["address"], PEM)
    faults = fake.faults if fault == "deep_health_bad" else fake.data(target["address"]).faults
    faults.add(fault)
    body = _deploy(flow, _runner(flow, target, tmp_path, fake), target, app_release(flow, target))
    assert body["status"] == "failed", body
    checks = {c["name"]: c for c in body["verification"]["checks"]}
    assert checks["native-version"]["status"] == checks["runtime-health"]["status"] == "passed"
    assert checks["app-deep-health"]["status"] == "failed"
    for part in evidence:
        assert part in checks["app-deep-health"]["evidence"]


def test_a_second_release_is_a_verified_patch_1_and_patch_130_is_refused(
    flow: Flow, owner_db: Database, tmp_path: Path
) -> None:
    target = app_target(flow)
    fake = snowflake_fake(target["address"], PEM)
    runner = _runner(flow, target, tmp_path, fake)
    first = app_release(flow, target)
    assert _deploy(flow, runner, target, first)["status"] == "verified"
    native = _native_ids(owner_db, flow.tenant)
    before = len(fake.writes())

    second = app_release(flow, target)
    assert second.release.digest != first.release.digest
    body = _deploy(flow, runner, target, second)
    assert body["status"] == "verified", body
    writes = fake.writes()[before:]
    assert sum(1 for w in writes if " ADD PATCH 1 FOR VERSION V1 " in w) == 1
    assert 'ALTER APPLICATION "SHIFDE_APP" UPGRADE USING VERSION V1 PATCH 1' in writes
    assert not any(w.startswith("CREATE APPLICATION ") for w in writes)
    assert _native_ids(owner_db, flow.tenant) == native
    # verified against the second release's digest, answered by patch 1's own files
    app = fake.state(target["address"]).applications["SHIFDE_APP"]
    patch = fake.state(target["address"]).versions["SHIFDE_PKG"][1]
    (stage,) = fake.state(target["address"]).stages.values()
    agent = stage["files"][f"{patch['digest']}/ui/config/agent.json"]["data"].decode()
    assert app["patch"] == 1 and second.release.digest in agent
    assert plan_row(owner_db, body["plan_id"])["release_digest"] == second.release.digest

    versions = fake.state(target["address"]).versions["SHIFDE_PKG"]
    for number in range(2, 130):
        versions.append({**versions[0], "patch": number, "created_on": f"seeded {number}"})
    sent = fake.writes()
    third = ask_plan(flow, target["id"], release_id=app_release(flow, target).id)
    assert runner.run_once()
    failed = plan_row(owner_db, third["id"])
    assert failed["status"] == "plan_failed"
    assert failed["status_reason"] == "plan job failed: snowflake_patch_limit"
    assert fake.writes() == sent and not any("DROP" in entry for entry in fake.log)
    assert _writes(owner_db, flow.tenant)[1] == 3
