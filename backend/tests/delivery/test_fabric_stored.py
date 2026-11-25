"""A Fabric row registered before `entra_tenant_id` was required (#406; changed as the schema owner,
registration refuses such bindings), the callers' API keys on a Fabric plan and decision (#352), and
the explicit naming of the Fabric kind: each mapping, its Fabric entry deleted in a copy, refuses,
and none ends in a Databricks default."""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any  # Any: JSON bodies, rows, functions
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.native.contracts import Target
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.delivery import plans as plan_module
from shifde.delivery import service
from shifde.executor import runner as runner_module
from shifde.executor.runner import Runner, toolchain_digest
from shifde.identity import apikeys
from tests.connections.routes import as_user
from tests.delivery.fabric import (
    approved_deployment,
    fabric_release,
    fabric_target,
    flow_runner,
    tenant_rows,
    workspace_of,
)
from tests.delivery.routes import (
    FABRIC_BINDINGS,
    Flow,
    bearer,
    deployment_row,
    enqueue,
    job_result,
    job_url,
    lease,
    lease_plan,
    make_due,
    owner_sql,
    plan_row,
    plan_value,
    plans,
    proof,
    register,
    request_plan,
    sp8_body,
    targets,
)
from tests.executor.support import ask_plan
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws_slug

UNUSABLE = {"unusable": True}
SCOPES = [
    "workspace:read",
    "project:read",
    "delivery:target:read",
    "delivery:plan:read",
    "delivery:plan:request",
    "delivery:plan:approve",
    "packaging:release:read",
    "packaging:solution:read",
]


def _age(owner_db: Database, target_id: object) -> None:
    """The target's row as a version before `entra_tenant_id` wrote it."""
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_targets SET bindings = bindings - 'entra_tenant_id' WHERE id = :g",
        g=target_id,
    )


def _names(n: int) -> dict[str, Any]:
    names = {"lakehouse_name": f"Lake{n}", "notebook_name": f"Notebook{n}"}
    return {"bindings": {**FABRIC_BINDINGS, **names}}


def _refused(body: dict[str, Any]) -> None:
    assert body["code"] == "target_bindings_invalid"
    assert "SalesLake" not in str(body) and "SalesNotebook" not in str(body)


# ------------------------------------------------------------------ a row of an earlier version
def test_an_earlier_fabric_row_is_listed_read_and_archived_as_unusable(
    flow: Flow, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    old = fabric_target(flow)
    good = fabric_target(flow)
    _age(flow.owner_db, old["id"])
    h = as_user(flow.client, flow.requester)
    r = flow.client.get(f"{targets(flow.slug)}?limit=200", headers=h)
    assert r.status_code == 200, r.text
    listed = {t["id"]: t["bindings"] for t in r.json()["items"]}
    assert listed == {old["id"]: UNUSABLE, good["id"]: good["bindings"]}
    for found, shown in ((old, UNUSABLE), (good, good["bindings"])):
        r = flow.client.get(f"{targets(flow.slug)}/{found['id']}", headers=h)
        assert (r.status_code, r.json()["bindings"]) == (200, shown), r.text
    r = flow.client.post(f"{targets(flow.slug)}/{old['id']}/archive", headers=h)
    assert (r.status_code, r.json()["status"]) == (200, "archived"), r.text
    assert r.json()["bindings"] == UNUSABLE and r.json()["revision"] == old["revision"] + 1
    r = flow.client.get(f"{targets(flow.slug)}?limit=200", headers=h)  # archived, still listed
    assert {t["id"]: t["bindings"] for t in r.json()["items"]} == listed
    assert f"delivery target {old['id']}: its stored bindings no longer validate" in caplog.text
    assert FABRIC_BINDINGS["lakehouse_name"] not in caplog.text
    # archiving freed the claim
    again = sp8_body("fabric", flow.workspace, old["address"])
    assert register(flow.client, h, flow.slug, again)["bindings"] == good["bindings"]


def test_an_earlier_fabric_rows_item_names_stay_bound_until_it_is_archived(flow: Flow) -> None:
    """`lock_claim` and the `binding_in_use` query read the row: its names still count (any case),
    other names are accepted on its workspace, and archiving it frees them."""
    old = fabric_target(flow)
    _age(flow.owner_db, old["id"])
    h = as_user(flow.client, flow.requester)
    other = sp8_body("fabric", flow.workspace, old["address"], **_names(1))
    register(flow.client, h, flow.slug, other)
    upper = {k: v.upper() for k, v in FABRIC_BINDINGS.items() if k.endswith("_name")}
    same = sp8_body("fabric", flow.workspace, old["address"])
    for body in (same, {**same, "bindings": {**FABRIC_BINDINGS, **upper}}):
        r = flow.client.post(targets(flow.slug), json=body, headers=h)
        assert (r.status_code, r.json()["code"]) == (409, "binding_in_use"), r.text
    r = flow.client.post(f"{targets(flow.slug)}/{old['id']}/archive", headers=h)
    assert r.status_code == 200, r.text
    register(flow.client, h, flow.slug, same)


def test_an_earlier_fabric_row_is_refused_a_plan_without_a_write(flow: Flow) -> None:
    old = fabric_target(flow)
    release = fabric_release(flow.owner_db, old["id"])
    _age(flow.owner_db, old["id"])
    before = tenant_rows(flow.owner_db, flow.tenant)
    h = as_user(flow.client, flow.requester)
    _refused(request_plan(flow.client, h, flow.slug, old["id"], release.id, 409))
    assert tenant_rows(flow.owner_db, flow.tenant) == before


def test_an_earlier_fabric_rows_plan_is_not_approved_and_can_be_rejected(flow: Flow) -> None:
    old = fabric_target(flow)
    release = fabric_release(flow.owner_db, old["id"])
    plan = flow.sealed(old["id"], release_id=release.id)
    _age(flow.owner_db, old["id"])
    before = tenant_rows(flow.owner_db, flow.tenant)
    h = as_user(flow.client, flow.approver)
    url = f"{plans(flow.slug)}/{plan['id']}"
    r = flow.client.post(f"{url}/approve", json={"plan_digest": plan["digest"]}, headers=h)
    assert r.status_code == 409, r.text
    _refused(r.json())
    assert tenant_rows(flow.owner_db, flow.tenant) == before
    assert plan_row(flow.owner_db, plan["id"])["status"] == "sealed"
    assert flow.client.post(f"{url}/reject", json={}, headers=h).status_code == 200


def test_the_job_context_of_an_earlier_fabric_row_is_refused(
    flow: Flow, app_db: Database, owner_db: Database
) -> None:
    made = fabric_target(flow)
    job = enqueue(app_db, flow.tenant, "observe", target_id=UUID(made["id"]))
    leased = lease(flow.client, flow.token).json()
    assert leased["job"]["id"] == str(job)

    def context() -> Any:  # Any: an httpx response
        return flow.client.get(
            job_url(job, "context"), params=proof(leased), headers=bearer(flow.token)
        )

    r = context()
    assert r.status_code == 200, r.text
    assert r.json()["target"]["host"] == "https://api.fabric.microsoft.com"
    assert r.json()["target"]["bindings"] == {**FABRIC_BINDINGS, "workspace": workspace_of(made)}
    _age(owner_db, made["id"])
    r = context()
    assert r.status_code == 409, r.text
    _refused(r.json())


def test_a_job_of_an_earlier_fabric_row_ends_without_a_request(flow: Flow, tmp_path: Path) -> None:
    """The executor: the plan job and, for a row changed after its plan, the apply job."""
    target, fake = fabric_target(flow), FakeFabricWorkspace()
    release = fabric_release(flow.owner_db, target["id"])
    runner = flow_runner(flow, target, tmp_path, fake)
    plan = ask_plan(flow, target["id"], release_id=release.id)
    _age(flow.owner_db, target["id"])
    assert runner.run_once()
    failed = plan_row(flow.owner_db, plan["id"])
    assert (failed["status"], failed["status_reason"]) == (
        "plan_failed",
        "plan job failed: execution_failed",
    )
    assert fake.requests == []
    second = fabric_target(flow)
    (tmp_path / "second").mkdir()
    runner = flow_runner(flow, second, tmp_path / "second", fake)
    release = fabric_release(flow.owner_db, second["id"])
    deployment = approved_deployment(flow, runner, second, release.id)
    _age(flow.owner_db, second["id"])
    seen = len(fake.requests)
    make_due(flow.owner_db, deployment)
    assert runner.run_once()
    row = deployment_row(flow.owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "no platform write was attempted")
    assert len(fake.requests) == seen


def test_an_earlier_fabric_row_is_an_unknown_id_to_the_other_client(
    client: TestClient, flow: Flow, world: World, people: People
) -> None:
    """Client A against client B: B's administrator gets an unknown id's 404 for A's row, and B's
    list, with a row of its own of that kind, names only its own; B cannot take A's workspace."""
    theirs = fabric_target(flow)
    _age(flow.owner_db, theirs["id"])
    h = as_user(client, people.owner_b)
    mine = register(client, h, world.b.slug, sp8_body("fabric", ws_slug(world.b), _address()))
    _age(flow.owner_db, mine["id"])
    before = tenant_rows(flow.owner_db, flow.tenant)
    bodies = []
    for target_id in (theirs["id"], uuid4()):
        answers = [
            client.get(f"{targets(world.b.slug)}/{target_id}", headers=h),
            client.post(f"{targets(world.b.slug)}/{target_id}/archive", headers=h),
        ]
        assert [r.status_code for r in answers] == [404, 404]
        bodies.append([{k: v for k, v in r.json().items() if k != "request_id"} for r in answers])
    assert bodies[0] == bodies[1]
    r = client.get(f"{targets(world.b.slug)}?limit=200", headers=h)
    assert {t["id"]: t["bindings"] for t in r.json()["items"]} == {mine["id"]: UNUSABLE}
    body = sp8_body("fabric", ws_slug(world.b), theirs["address"], **_names(2))
    r = client.post(targets(world.b.slug), json=body, headers=h)
    assert (r.status_code, r.json()["code"]) == (409, "platform_already_registered"), r.text
    assert tenant_rows(flow.owner_db, flow.tenant) == before


def _address() -> str:
    return f"https://api.fabric.microsoft.com/v1/workspaces/{uuid4()}"


# ------------------------------------------------------------------ the callers' API keys (#352)
def _key(flow: Flow, app_db: Database, settings: Settings, email: str) -> tuple[UUID, str]:
    """An API key of the person `email`, who made it: (id, secret)."""
    with flow.owner_db.session() as o:
        user = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
    with app_session(app_db, flow.tenant, user) as s:
        row, secret = apikeys.mint(
            s,
            settings,
            tenant_id=flow.tenant,
            name=f"key-{uuid4().hex[-12:]}",
            principal_type="user",
            principal_id=user,
            scopes=SCOPES,
            resource_scope=None,
            expires_at=utcnow() + timedelta(days=1),
            created_by=user,
        )
        made = (row.id, secret)
        s.commit()
    return made


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_fabric_plan_and_its_decision_record_the_callers_api_keys(
    flow: Flow, app_db: Database, settings: Settings, decision: str
) -> None:
    target = fabric_target(flow)
    release = fabric_release(flow.owner_db, target["id"])
    asking, asking_secret = _key(flow, app_db, settings, flow.requester)
    deciding, deciding_secret = _key(flow, app_db, settings, flow.approver)
    made = request_plan(flow.client, bearer(asking_secret), flow.slug, target["id"], release.id)
    assert plan_row(flow.owner_db, made["id"])["requested_api_key_id"] == asking
    leased = lease_plan(flow.client, flow.token)
    r = job_result(flow.client, flow.token, leased, status="succeeded", value=plan_value())
    assert r.status_code == 200, r.text
    sealed = flow.get_plan(made["id"])
    r = flow.client.post(
        f"{plans(flow.slug)}/{made['id']}/{decision}",
        json={"plan_digest": sealed["digest"]} if decision == "approve" else {},
        headers=bearer(deciding_secret),
    )
    assert r.status_code == 200, r.text
    with flow.owner_db.session() as o:
        stored = o.execute(
            text(
                "SELECT decision, decided_api_key_id FROM shifde.delivery_approvals "
                "WHERE plan_id = :p"
            ),
            {"p": made["id"]},
        ).all()
    assert [tuple(row) for row in stored] == [(decision, deciding)]
    # a session names no key
    on_session = flow.sealed(target["id"], "other-app", release_id=release.id)
    assert plan_row(flow.owner_db, on_session["id"])["requested_api_key_id"] is None


# ------------------------------------------------------------------ the Fabric kind is named
class _NoFabric(ast.NodeTransformer):
    """Deletes each `if`/`elif` branch whose condition names Fabric."""

    def __init__(self) -> None:
        self.deleted = 0

    def visit_If(self, node: ast.If) -> Any:  # Any: a node, or the statements after it
        self.generic_visit(node)
        if "fabric" in ast.unparse(node.test).lower():
            self.deleted += 1
            return node.orelse or []
        return node


def _without_fabric(function: Any, module: ModuleType) -> Callable[..., Any]:
    """A copy of `function` without its one Fabric branch, in a copy of its module's names."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    cut = _NoFabric()
    cut.visit(tree)
    assert cut.deleted == 1
    names = dict(vars(module))
    flags = annotations.compiler_flag
    exec(compile(ast.fix_missing_locations(tree), "<copy>", "exec", flags), names)  # noqa: S102
    copy = names[function.__name__]
    return copy.__func__ if isinstance(copy, staticmethod) else copy


def _row(kind: str, **values: Any) -> dict[str, Any]:
    return {
        "tenant_id": uuid4(),
        "id": uuid4(),
        "revision": 1,
        "environment": "dev",
        "platform_kind": kind,
        "address": _address(),
        "bindings": dict(FABRIC_BINDINGS),
        **values,
    }


def test_the_adapter_target_names_fabric_and_refuses_another_kind() -> None:
    row = _row("fabric")
    host, workspace = row["address"].split("/v1/workspaces/")
    assert service.adapter_target(row) == Target(
        tenant_id=row["tenant_id"],
        id=row["id"],
        revision=1,
        environment="dev",
        host=host,
        bindings={**FABRIC_BINDINGS, "workspace": workspace},
    )
    copy = _without_fabric(service.adapter_target, service)
    databricks = _row("databricks", address="https://adb-1.cloud.databricks.com")
    assert copy(databricks) == service.adapter_target(databricks)  # the copy works
    for refused, kind in ((copy, "fabric"), (service.adapter_target, "fabrics")):
        with pytest.raises(ValueError, match="no adapter target for this platform kind"):
            refused(_row(kind))


def test_the_toolchain_digest_names_fabric_and_refuses_another_kind() -> None:
    copy = _without_fabric(toolchain_digest, runner_module)
    assert copy("fake", "databricks") == toolchain_digest("fake", "databricks")
    unknown: Any = "fabrics"
    for refused, kind in ((copy, "fabric"), (toolchain_digest, unknown)):
        with pytest.raises(ValueError, match="no toolchain for this kind of target"):
            refused("fake", kind)


class _AnyPlan:
    plan = object()


def test_the_plan_details_name_fabric_and_refuse_another_adapter(tmp_path: Path) -> None:
    platform = runner_module.FakePlatform(environment="test", fabric_enabled=True)
    target = service.adapter_target(_row("fabric"))
    adapter = platform.fabric_adapter(target, lambda: pytest.fail("no credential is needed"))
    ctx: Any = _AnyPlan()
    copy = _without_fabric(Runner._details, runner_module)
    for details, given in ((copy, adapter), (Runner._details, object())):
        with pytest.raises(ValueError, match="no plan details for this kind of adapter"):
            details(given, ctx, None, uuid4(), None)


def test_no_dispatch_entry_means_no_fabric_platform(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, fake = fabric_target(flow), FakeFabricWorkspace()
    release = fabric_release(flow.owner_db, target["id"])
    runner = flow_runner(flow, target, tmp_path, fake)
    copy = {k: v for k, v in runner_module.DISPATCH.items() if k != "fabric"}
    monkeypatch.setattr(runner_module, "DISPATCH", copy)
    plan = ask_plan(flow, target["id"], release_id=release.id)
    assert runner.run_once()
    failed = plan_row(flow.owner_db, plan["id"])
    assert (failed["status"], failed["status_reason"]) == (
        "plan_failed",
        "plan job failed: execution_failed",
    )
    assert fake.requests == []


def _ask_refused(flow: Flow, target: dict[str, Any], release_id: object, code: str) -> None:
    before = tenant_rows(flow.owner_db, flow.tenant)
    h = as_user(flow.client, flow.requester)
    assert request_plan(flow.client, h, flow.slug, target["id"], release_id, 409)["code"] == code
    flow.client.cookies.clear()
    assert tenant_rows(flow.owner_db, flow.tenant) == before


def test_no_plan_entry_or_branch_means_no_fabric_plan(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = fabric_target(flow)
    release = fabric_release(flow.owner_db, target["id"])
    kinds = plan_module.PLATFORM_OF_KIND
    with monkeypatch.context() as patch:
        copy = {k: v for k, v in kinds.items() if k != "fabric.solution/v1"}
        patch.setattr(plan_module, "PLATFORM_OF_KIND", copy)
        _ask_refused(flow, target, release.id, "target_kind_mismatch")
    with monkeypatch.context() as patch:
        request = _without_fabric(plan_module.PlanService.request, plan_module)
        patch.setattr(plan_module.PlanService, "request", request)
        _ask_refused(flow, target, release.id, "generator_version_unsupported")
    # behind the kind gate, another kind on a Fabric target and a Fabric kind elsewhere
    databricks = flow.release(target["id"])
    with monkeypatch.context() as patch:
        patch.setattr(plan_module, "PLATFORM_OF_KIND", {**kinds, "databricks.app/v1": "fabric"})
        _ask_refused(flow, target, databricks, "generator_version_unsupported")
    elsewhere = flow.target()
    solution = fabric_release(flow.owner_db, elsewhere["id"])
    with monkeypatch.context() as patch:
        patch.setattr(
            plan_module, "PLATFORM_OF_KIND", {**kinds, "fabric.solution/v1": "databricks"}
        )
        _ask_refused(flow, elsewhere, solution.id, "generator_version_unsupported")


def test_no_required_checks_entry_means_never_verified(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, fake = fabric_target(flow), FakeFabricWorkspace()
    runner = flow_runner(flow, target, tmp_path, fake)
    release = fabric_release(flow.owner_db, target["id"])
    deployment = approved_deployment(flow, runner, target, release.id)
    copy = {k: v for k, v in service.REQUIRED_BY_PLATFORM.items() if k != "fabric"}
    monkeypatch.setattr(service, "REQUIRED_BY_PLATFORM", copy)
    for _ in ("apply", "observe", "verify"):
        make_due(flow.owner_db, deployment)
        assert runner.run_once()
    assert deployment_row(flow.owner_db, deployment)["status"] == "failed"
