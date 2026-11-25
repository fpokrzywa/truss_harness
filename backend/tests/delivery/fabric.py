"""Fabric delivery test helpers (SP8-5): a Fabric target, a sealed, passed `/1`
`fabric.solution/v1` release in its scope (inserted as the schema owner, like `releases`), a runner
over the in-process gateway and a switched-on Fabric fake, and Fabric receipts."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any  # Any: JSON bodies and manifests
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.executor.client import GatewayClient
from shifde.executor.credentials import CredentialBroker
from shifde.executor.runner import FakePlatform, Runner
from shifde.packaging.canonical import digest
from tests.connections.routes import as_user
from tests.delivery.releases import SealedRelease, insert_run, release_for_target
from tests.delivery.routes import Flow, make_due, register, sp8_address, sp8_body
from tests.executor.support import ask_plan, sp8_credentials_file

KIND = "fabric.solution/v1"
CHECKS = ("native-items", "runtime-health")
LAKEHOUSE, NOTEBOOK = "fabric.Lakehouse", "fabric.Notebook"


def workspace_of(target: dict[str, Any]) -> str:
    """The workspace GUID of a registered Fabric target."""
    return str(target["address"]).rsplit("/", 1)[1]


def fabric_target(flow: Flow, url: str | None = None, **overrides: Any) -> dict[str, Any]:
    """A Fabric target of the flow's organization, on a workspace of its own unless `url`."""
    h = as_user(flow.client, flow.requester)
    body = sp8_body("fabric", flow.workspace, url or sp8_address("fabric"), **overrides)
    made: dict[str, Any] = register(flow.client, h, flow.slug, body)
    flow.client.cookies.clear()
    return made


def fabric_release(
    owner_db: Database,
    target_id: object,
    *,
    evaluation: str | None = "passed",
    kind: str = KIND,
    edit: Callable[[dict[str, Any]], None] | None = None,
) -> SealedRelease:
    """A sealed release of `kind` (Fabric unless given) in the target's scope, with an evaluation
    run of `evaluation` status on its digests; `edit` changes the manifest before it is digested.
    It is a second release of `release_for_target`'s solution version (which has no run)."""
    base = release_for_target(owner_db, target_id, evaluation=None)
    release_id = uuid7()
    manifest = {
        **base.manifest,
        "target_kind": kind,
        "compiler_version": "shifde.packaging/1",
        "nonce": str(release_id),
    }
    if edit is not None:
        edit(manifest)
    release_digest = digest(manifest)
    with owner_db.session() as o:
        set_tenant(o, base.tenant)
        o.execute(
            text(
                "INSERT INTO shifde.releases (tenant_id, id, solution_version_id, digest, "
                "manifest, compatibility, status, sealed_by) SELECT tenant_id, :i, "
                "solution_version_id, :d, CAST(:m AS jsonb), '{}'::jsonb, 'sealed', sealed_by "
                "FROM shifde.releases WHERE id = :b"
            ),
            {"i": release_id, "d": release_digest, "m": json.dumps(manifest), "b": base.id},
        )
        o.commit()
    made = SealedRelease(
        base.tenant,
        release_id,
        release_digest,
        manifest,
        base.dataset_id,
        base.dataset_digest,
        None,
    )
    if evaluation is not None:
        insert_run(owner_db, made, evaluation, base.dataset_digest)
    return made


def fabric_platform(fake: FakeFabricWorkspace) -> FakePlatform:
    return FakePlatform(fabric=fake, environment="test", fabric_enabled=True)


def fabric_runner(
    client: TestClient, token: str, credentials: Path, platform: Any, gateway: Any = None
) -> Runner:  # Any: a platform, a GatewayClient stand-in
    """A runner of the executor holding `token`, over the in-process gateway."""
    used = gateway or GatewayClient("https://testserver", token, http=client)
    me = used.whoami()
    return Runner(
        used,
        CredentialBroker.from_file(credentials),
        me.executor_id,
        platform,
        tenant_id=me.tenant_id,
    )


def flow_runner(
    flow: Flow, target: dict[str, Any], directory: Path, fake: FakeFabricWorkspace
) -> Runner:
    """The flow's runner with a credentials entry of `target`, against `fake`."""
    creds = sp8_credentials_file(directory, "fabric", target["address"], tenant=flow.tenant)
    return fabric_runner(flow.client, flow.token, creds, fabric_platform(fake))


def approved_deployment(
    flow: Flow, runner: Runner, target: dict[str, Any], release_id: object
) -> str:
    """A plan of the release sealed by the runner and approved; its queued deployment's id."""
    plan = ask_plan(flow, target["id"], release_id=release_id)
    assert runner.run_once()
    sealed = flow.get_plan(plan["id"])
    assert sealed["status"] == "sealed", sealed
    return str(flow.approve(sealed)["deployment_id"])


def run_due(flow: Flow, runner: Runner, deployment: str, *kinds: str) -> None:
    for _ in kinds:
        make_due(flow.owner_db, deployment)
        assert runner.run_once()


def receipt(
    tenant: object, target: dict[str, Any], artifact_digest: str, **changes: Any
) -> dict[str, Any]:  # Any: JSON
    """A Fabric `ApplyReceipt` value: one lakehouse and one notebook of the target's workspace,
    `platform: fake`; `changes` replace its fields."""
    application = str(uuid4())
    value: dict[str, Any] = {
        "tenant_id": str(tenant),
        "application_id": application,
        "target_id": target["id"],
        "artifact_digest": artifact_digest,
        "resources": [
            {
                "key": key,
                "native_id": f"fabric:{workspace_of(target)}:{uuid4()}",
                "resource_type": resource_type,
                "application_id": application,
                "target_id": target["id"],
            }
            for key, resource_type in (("lakehouse", LAKEHOUSE), ("notebook", NOTEBOOK))
        ],
        "operation_handles": {"platform": "fake"},
    }
    value.update(changes)
    return value


TABLES = {
    "delivery_targets": "id",
    "delivery_artifacts": "id",
    "delivery_plans": "id",
    "delivery_deployments": "id",
    "delivery_jobs": "id",
    "delivery_approvals": "id",
    "delivery_journal": "deployment_id, step_key",
    "delivery_native_resources": "target_id, native_id",
}


def tenant_rows(owner_db: Database, tenant: UUID) -> list[list[tuple[Any, ...]]]:
    """Every delivery row of `tenant`, read as the schema owner."""
    with owner_db.session() as o:
        return [
            [
                tuple(r)
                for r in o.execute(
                    # the table and column names are the literals above
                    text(f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY {order}"),  # nosec B608
                    {"t": tenant},
                )
            ]
            for table, order in TABLES.items()
        ]
