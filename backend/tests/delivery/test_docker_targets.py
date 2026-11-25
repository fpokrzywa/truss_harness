"""Docker targets and plans of one organization through the routes (SP8-9): the normalized
`workspace_host`, `binding_in_use` on one claim, `holds_claim`, the five plan gates in their order
(each before any write), the stored bundle and the plan digest, the approval's claim check, the job
context's string port and the server's verification rule. Client A against client B is in
`test_routes_cross_client.py`, the executor's half in `tests/executor/test_runner_docker.py`."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.native.addresses import AddressRefused, address_key
from shifde.adapters.native.base import read_archive
from shifde.adapters.native.contracts import Target
from shifde.adapters.native.docker.source import validate_bundle, validate_for_target
from shifde.appgen.render import render
from shifde.appgen.render_docker import DockerTargetValues
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery import repository, service
from shifde.delivery.service import TargetService
from tests.appgen.rendered import DOCKER_SEALS, ENDPOINT, on_docker
from tests.connections.routes import as_user, limited_user
from tests.delivery.app_releases import add_provider
from tests.delivery.docker import (
    docker_release,
    docker_target,
    rows,
    sp8_target,
    workspace,
    workspace_target,
)
from tests.delivery.routes import (
    DOCKER_BINDINGS,
    Flow,
    address,
    bearer,
    counts,
    delivery_client,
    deployment_row,
    job_result,
    job_url,
    lease_kind,
    lease_plan,
    make_due,
    owner_sql,
    plan_row,
    plan_value,
    plans,
    register,
    request_plan,
    sp8_address,
    sp8_body,
    target_body,
    targets,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws2_slug, ws_slug

FIXED = {
    "workspace_not_claimed": (
        "The target's Databricks workspace is not registered as a Databricks target of this "
        "organization."
    ),
    "serving_endpoint_mismatch": (
        "The release's model binding is not the target's serving endpoint."
    ),
    "model_not_in_target_workspace": (
        "The release's model provider is not in the target's workspace."
    ),
}


def _bindings(**overrides: Any) -> dict[str, Any]:
    return {**DOCKER_BINDINGS, **overrides}


def _refused(flow: Flow, target: dict[str, Any], release_id: object, code: str) -> dict[str, Any]:
    """The plan request's 409 `code`; no delivery row was written or changed."""
    before = rows(flow.owner_db, flow.tenant)
    h = as_user(flow.client, flow.requester)
    body: dict[str, Any] = request_plan(
        flow.client, h, flow.slug, target["id"], release_id, status=409
    )
    flow.client.cookies.clear()
    assert body["code"] == code, body
    if code in FIXED:
        assert body["detail"] == FIXED[code]
    assert rows(flow.owner_db, flow.tenant) == before
    return body


def _ask(flow: Flow, target: dict[str, Any], release_id: object) -> dict[str, Any]:
    h = as_user(flow.client, flow.requester)
    made: dict[str, Any] = request_plan(flow.client, h, flow.slug, target["id"], release_id)
    flow.client.cookies.clear()
    return made


def _decide(flow: Flow, plan: dict[str, Any], decision: str) -> httpx.Response:
    h = as_user(flow.client, flow.approver)
    sent = {"plan_digest": plan["digest"]} if decision == "approve" else {}
    r = flow.client.post(f"{plans(flow.slug)}/{plan['id']}/{decision}", json=sent, headers=h)
    flow.client.cookies.clear()
    return r


def _archive(flow: Flow, target_id: object) -> None:
    h = as_user(flow.client, flow.requester)
    r = flow.client.post(f"{targets(flow.slug)}/{target_id}/archive", headers=h)
    flow.client.cookies.clear()
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ bindings
@pytest.mark.parametrize(
    "host",
    [
        "https://10.0.0.1",
        "https://workspace-of-mine.example.com",
        "https://cloud.databricks.com",
        "https://acct-x.snowflakecomputing.com",
    ],
)
def test_a_docker_workspace_host_must_be_a_databricks_address(
    client: TestClient, world: World, people: People, owner_db: Database, host: str
) -> None:
    h = as_user(client, people.owner_a)
    body = sp8_body(
        "docker", ws_slug(world.a), sp8_address("docker"), bindings=_bindings(workspace_host=host)
    )
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_error"
    (error,) = r.json()["errors"]
    assert error["loc"] == ["body", "bindings", "workspace_host"]
    assert error["msg"] == r.json()["detail"] == str(AddressRefused())
    assert host not in r.text and host.removeprefix("https://") not in r.text
    assert counts(owner_db, world.a.tenant) == (0, 0)


def test_the_docker_workspace_host_is_checked_after_the_permission(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    email, _ = limited_user(owner_db, world.a.tenant, ["delivery:target:read", "workspace:read"])
    h = as_user(client, email)
    body = sp8_body(
        "docker",
        ws_slug(world.a),
        sp8_address("docker"),
        bindings=_bindings(workspace_host="https://10.0.0.1"),
    )
    r = client.post(targets(world.a.slug), headers=h, json=body)
    assert r.status_code in (403, 404), r.text
    assert "errors" not in r.json() or not r.json()["errors"]


def test_a_configured_databricks_suffix_is_accepted_for_the_workspace_host_and_the_claim(
    settings: Settings, flow: Flow
) -> None:
    """`SHIFDE_DELIVERY_DATABRICKS_HOST_SUFFIXES` counts at registration and at the plan gate."""
    host = address("dbx.corp.example")
    update = {"delivery_databricks_host_suffixes": ["dbx.corp.example"]}
    with delivery_client(settings) as c:
        h = as_user(c, flow.requester)
        body = sp8_body(
            "docker",
            flow.workspace,
            sp8_address("docker"),
            bindings=_bindings(workspace_host=host),
        )
        register(c, h, flow.slug, body, status=422)
    with delivery_client(settings.model_copy(update=update)) as c:
        custom = dataclasses.replace(flow, client=c)
        workspace_target(custom, host)
        target = docker_target(custom, workspace_host=host)
        assert target["bindings"]["workspace_host"] == host
        assert _ask(custom, target, docker_release(custom, target).id)["status"] == "requested"


@pytest.mark.parametrize("reused", ["project", "port"])
def test_a_reused_docker_project_or_port_on_one_claim_is_binding_in_use(
    client: TestClient, world: World, people: People, owner_db: Database, reused: str
) -> None:
    a = world.a
    h = as_user(client, people.owner_a)
    url = sp8_address("docker")
    first = register(client, h, a.slug, sp8_body("docker", ws_slug(a), url))
    bindings = _bindings(project="other-app", port=8502)
    bindings[reused] = DOCKER_BINDINGS[reused]
    body = sp8_body("docker", ws2_slug(a), url, bindings=bindings)
    r = client.post(targets(a.slug), headers=h, json=body)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "binding_in_use"
    assert r.json()["detail"] == (
        "Another active target on this Docker daemon already binds this project or port."
    )
    assert first["id"] not in r.text and first["name"] not in r.text
    assert counts(owner_db, a.tenant) == (1, 1)
    # another daemon may bind the same, and so may a distinct target of the same daemon
    register(client, h, a.slug, sp8_body("docker", ws2_slug(a), sp8_address("docker")))
    distinct = _bindings(project="third-app", port=8503)
    register(client, h, a.slug, sp8_body("docker", ws_slug(a), url, bindings=distinct))
    # archiving frees the names on the claim
    assert client.post(f"{targets(a.slug)}/{first['id']}/archive", headers=h).is_success
    assert register(client, h, a.slug, body)["bindings"] == bindings


def test_the_docker_binding_check_runs_under_the_claims_lock(
    client: TestClient, world: World, people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    lock, check = repository.lock_claim, repository.docker_binding_in_use

    def locked(*args: Any, **kwargs: Any) -> Any:
        calls.append("lock")
        return lock(*args, **kwargs)

    def checked(*args: Any, **kwargs: Any) -> Any:
        calls.append("check")
        return check(*args, **kwargs)

    monkeypatch.setattr(repository, "lock_claim", locked)
    monkeypatch.setattr(repository, "docker_binding_in_use", checked)
    h = as_user(client, people.owner_a)
    register(client, h, world.a.slug, sp8_body("docker", ws_slug(world.a), sp8_address("docker")))
    assert calls == ["lock", "check"]


# ------------------------------------------------------------------ holds_claim
def _holds(app_db: Database, tenant: UUID, kind: str, url: str) -> bool:
    """`TargetService.holds_claim` as `shifde_app` in `tenant`'s context; it writes nothing."""
    with app_session(app_db, tenant) as s:
        scope: Any = SimpleNamespace(session=s, tenant_id=tenant)  # stands in for a TenantScope
        held = TargetService(scope).holds_claim(kind, url)
        assert s.execute(text("SELECT pg_current_xact_id_if_assigned()")).scalar_one() is None
        s.rollback()
    return held


def test_holds_claim_is_true_only_for_an_active_claim_of_the_kind(
    flow: Flow, app_db: Database
) -> None:
    active, archived = workspace_target(flow), workspace_target(flow)
    daemon = sp8_target(flow, "docker")["address"]
    assert _holds(app_db, flow.tenant, "databricks", active["address"])
    assert _holds(app_db, flow.tenant, "databricks", active["address"].upper() + ":443/")
    assert _holds(app_db, flow.tenant, "databricks", archived["address"])
    assert _holds(app_db, flow.tenant, "docker", daemon)
    _archive(flow, archived["id"])
    assert not _holds(app_db, flow.tenant, "databricks", archived["address"])
    assert not _holds(app_db, flow.tenant, "databricks", address())
    # an address of another kind, refused or not
    assert not _holds(app_db, flow.tenant, "databricks", daemon)
    assert not _holds(app_db, flow.tenant, "docker", active["address"])
    assert not _holds(app_db, flow.tenant, "databricks", "https://10.0.0.1")
    for kind in ("snowflake", "palantir", "fabric"):
        assert not _holds(app_db, flow.tenant, kind, active["address"])


def test_holds_claim_needs_an_active_target_on_the_claim(flow: Flow, app_db: Database) -> None:
    """A claim row without any target (no route leaves one) is not held."""
    url = address()
    with app_session(app_db, flow.tenant, uuid7()) as s:
        assert repository.claim_platform(s, "databricks", address_key(url)) is not None
        s.commit()
    assert not _holds(app_db, flow.tenant, "databricks", url)


# ------------------------------------------------------------------ plan gates
def test_the_docker_plan_gates_come_in_order_and_write_nothing(flow: Flow) -> None:
    """Releases failing several checks: the first failing one answers."""
    claimed = workspace(flow)
    elsewhere = add_provider(
        flow.owner_db,
        flow.tenant,
        "openai_compatible",
        f"{workspace(flow)}/serving-endpoints",  # a workspace of A, not the target's
    )

    def tampered(manifest: dict[str, Any]) -> None:
        manifest["pins"][0]["spec_hash"] = "0" * 64
        on_docker(DOCKER_SEALS["databricks"])(manifest)

    unclaimed = docker_target(flow, workspace_host=address(), serving_endpoint="other-endpoint")
    mismatched = docker_target(flow, workspace_host=claimed, serving_endpoint="other-endpoint")
    bound = docker_target(flow, workspace_host=claimed)
    every = docker_release(flow, bound, provider_id=elsewhere, edit=tampered)
    _refused(flow, unclaimed, every.id, "workspace_not_claimed")
    _refused(flow, mismatched, every.id, "serving_endpoint_mismatch")
    body = _refused(flow, bound, every.id, "model_not_in_target_workspace")
    assert "adb-" not in str(body)
    changed = docker_release(flow, bound, edit=tampered)
    _refused(flow, bound, changed.id, "release_source_changed")
    for app in (a for a in DOCKER_SEALS.values() if a != DOCKER_SEALS["docker"]):
        sealed = docker_release(flow, bound, edit=on_docker(app))
        _refused(flow, bound, sealed.id, "generator_version_unsupported")


def test_a_docker_release_not_of_the_app_compiler_never_takes_the_template_path(
    flow: Flow,
) -> None:
    target = docker_target(flow, workspace_host=workspace(flow))

    def first_compiler(manifest: dict[str, Any]) -> None:
        manifest["compiler_version"] = "shifde.packaging/1"

    rel = docker_release(flow, target, edit=first_compiler)
    body = _refused(flow, target, rel.id, "generator_version_unsupported")
    assert body["detail"] == "the release is not a generated app release"


def test_the_model_provider_is_compared_with_the_workspace_host(flow: Flow) -> None:
    host, other = workspace(flow), workspace(flow)
    target = docker_target(flow, workspace_host=host)
    for base, accepted in (
        (f"{host}/serving-endpoints/{ENDPOINT}/invocations", True),
        (f"{other}/serving-endpoints/{ENDPOINT}/invocations", False),
        (target["address"], False),
    ):
        provider = add_provider(flow.owner_db, flow.tenant, "openai_compatible", base)
        rel = docker_release(flow, target, provider_id=provider)
        if accepted:
            assert _ask(flow, target, rel.id)["status"] == "requested"
        else:
            _refused(flow, target, rel.id, "model_not_in_target_workspace")


def test_a_docker_release_needs_exactly_one_model_binding(flow: Flow) -> None:
    target = docker_target(flow, workspace_host=workspace(flow))

    def two(manifest: dict[str, Any]) -> None:
        bound = manifest["bindings"]["model_bindings"]
        bound["second"] = dict(bound["default"])

    def none(manifest: dict[str, Any]) -> None:
        manifest["bindings"]["model_bindings"] = {}

    for edit in (two, none):
        rel = docker_release(flow, target, edit=edit)
        _refused(flow, target, rel.id, "serving_endpoint_mismatch")


def test_the_claim_is_the_organizations_not_the_projects(
    flow: Flow, world: World, people: People
) -> None:
    """Design section 10 item 40: a workspace registered in another SETH workspace counts."""
    host = address()
    h = as_user(flow.client, people.owner_a)
    register(flow.client, h, flow.slug, target_body(ws2_slug(world.a), host))
    flow.client.cookies.clear()
    target = docker_target(flow, workspace_host=host)
    assert _ask(flow, target, docker_release(flow, target).id)["status"] == "requested"


# ------------------------------------------------------------------ accepted plans
def test_an_accepted_docker_plan_stores_the_targets_bundle(flow: Flow, owner_db: Database) -> None:
    target = docker_target(flow, workspace_host=workspace(flow), warehouse_id="123e4", port=9123)
    rel = docker_release(flow, target)
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
    assert artifact["tenant_id"] == flow.tenant and artifact["target_kind"] == "docker.compose/v1"
    files = read_archive(bytes(artifact["content"]))
    values = DockerTargetValues(
        target_id=UUID(target["id"]), port=9123, warehouse_id="123e4", serving_endpoint=ENDPOINT
    )
    assert files == render(rel.release.manifest, rel.release.digest, rel.resolved(), values)
    bundle = validate_bundle(files)
    assert bundle.release_digest == rel.release.digest
    native = Target(
        tenant_id=flow.tenant,
        id=UUID(target["id"]),
        host=target["address"],
        bindings={"port": "9123"},
    )
    assert validate_for_target(bundle, native) == (f"shifde-{UUID(target['id']).hex}", 9123)
    # neither the workspace host, the daemon nor the display name is in the bundle's own files
    compose = files["compose.yaml"].decode() + files["Dockerfile"].decode()
    for hidden in (target["bindings"]["workspace_host"], target["address"], "sales-app"):
        assert hidden not in compose

    leased = lease_plan(flow.client, flow.token)
    value = plan_value()
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=value)
    assert r.status_code == 200, r.text
    row = plan_row(owner_db, made["id"])
    common = {
        "artifact_digest": row["artifact_digest"],
        "target_revision": row["target_revision"],
        "snapshot_fingerprint": value["snapshot"]["fingerprint"],
        "steps": row["steps"],
        "adapter_version": value["adapter_version"],
        "toolchain_digest": value["toolchain_digest"],
        "release_digest": rel.release.digest,
    }
    assert row["digest"] == service.plan_digest(
        **common, generator_version="shifde.appgen.docker/1.0"
    )
    assert row["digest"] != service.plan_digest(**common)


def test_the_docker_job_context_gives_the_port_as_a_string(flow: Flow) -> None:
    host = workspace(flow)
    target = docker_target(flow, workspace_host=host, port=9124)
    _ask(flow, target, docker_release(flow, target).id)
    leased = lease_plan(flow.client, flow.token)
    sent = {"lease_id": leased["lease_id"], "fence": leased["fence"]}
    r = flow.client.get(
        job_url(leased["job"]["id"], "context"), params=sent, headers=bearer(flow.token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["platform_kind"] == "docker"
    assert r.json()["plan"]["generator_version"] == "shifde.appgen.docker/1.0"
    assert r.json()["target"]["bindings"] == _bindings(port="9124", workspace_host=host)


# ------------------------------------------------------------------ approval
def test_approval_checks_the_claim_again_and_reject_does_not(flow: Flow) -> None:
    held = workspace_target(flow)
    target = docker_target(flow, workspace_host=held["address"])
    rel = docker_release(flow, target)
    sealed = flow.sealed(target["id"], release_id=rel.id)
    other = flow.sealed(target["id"], release_id=rel.id)
    _archive(flow, held["id"])
    before = rows(flow.owner_db, flow.tenant)
    r = _decide(flow, sealed, "approve")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "workspace_not_claimed"
    assert r.json()["detail"] == FIXED["workspace_not_claimed"]
    assert rows(flow.owner_db, flow.tenant) == before  # no approval, deployment or job
    assert plan_row(flow.owner_db, sealed["id"])["status"] == "sealed"
    rejected = _decide(flow, other, "reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected", rejected.text
    # registered again by this organization: the same sealed plan is approved
    workspace_target(flow, held["address"])
    approved = _decide(flow, sealed, "approve")
    assert approved.status_code == 200 and approved.json()["status"] == "approved", approved.text


# ------------------------------------------------------------------ server verification rule
CHECKS = ("native-version", "runtime-health", "app-deep-health")


@pytest.mark.parametrize("missing", [None, *CHECKS])
@pytest.mark.parametrize("how", ["omitted", "failed", "skipped"])
def test_a_docker_deployment_is_verified_only_with_its_three_checks(
    flow: Flow, owner_db: Database, missing: str | None, how: str
) -> None:
    target = docker_target(flow, workspace_host=workspace(flow))
    sealed = flow.sealed(target["id"], release_id=docker_release(flow, target).id)
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
    leased = lease_kind(flow.client, flow.token, "verify")
    checks = [{"name": name, "status": "passed"} for name in CHECKS if name != missing]
    if missing is not None and how != "omitted":
        checks.append({"name": missing, "status": how})
    r = job_result(flow.client, flow.token, leased, status="succeeded", value={"checks": checks})
    assert r.status_code == 200, r.text
    status = "verified" if missing is None else "failed"
    assert deployment_row(owner_db, deployment)["status"] == status


def test_a_docker_deployment_without_a_generated_release_is_never_verified() -> None:
    assert service.REQUIRED_BY_PLATFORM["docker"] == (None, CHECKS)


def test_a_seal_result_on_an_archived_docker_target_fails_the_plan(
    flow: Flow, owner_db: Database
) -> None:
    target = docker_target(flow, workspace_host=workspace(flow))
    made = _ask(flow, target, docker_release(flow, target).id)
    leased = lease_plan(flow.client, flow.token)
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_targets SET status = 'archived', claim_id = NULL WHERE id = :i",
        i=target["id"],
    )
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=plan_value())
    assert r.status_code == 200, r.text
    assert plan_row(owner_db, made["id"])["status"] == "plan_failed"
