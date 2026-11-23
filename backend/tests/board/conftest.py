"""Board tests (sub-project 7 design 7, 9.1 and 9.7): `board.api.router` behind `require_auth` on
the identity harness app, with the tenancy route-test world (two clients, fresh slugs and ids per
test) and its people.

The board only reads, so its rows are written here as the schema owner: solutions, versions and
releases (the first through `tests.delivery.releases.make_release`), evaluation runs, archived
targets (no platform claim), plans and deployments in any status. Every route runs as
`shifde_app` under row-level security.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any  # Any: TestClient options, JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import text

from shifde.board import api
from shifde.core.db import Database
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.identity.dependencies import require_auth
from tests.conftest import CLEAN_TABLES
from tests.connections.routes import grant
from tests.delivery.releases import make_release
from tests.delivery.routes import address
from tests.identity import harness
from tests.tenancy.conftest import World, owner_member, world
from tests.tenancy.routes import People, make_people

__all__ = ["world"]

for _table in (
    "delivery_deployments",
    "delivery_plans",
    "delivery_artifacts",
    "evaluation_runs",
    "evaluation_datasets",
    "releases",
    "solution_versions",
    "solutions",
    "artifact_versions",
    "artifacts",
    "delivery_targets",
    "login_lockouts",
    "user_identities",
    "sessions",
    "local_credentials",
    "memberships",
    "users",
    "audit_events",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)

_VARYING = {"instance", "request_id"}


@contextmanager
def board_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with board_client(settings) as c:
        yield c


@pytest.fixture
def people(owner_db: Database, world: World) -> People:
    with owner_db.session() as owner:
        return make_people(owner, world)


# ------------------------------------------------------------------ requests
def org_board(slug: str) -> str:
    return f"/api/v1/orgs/{slug}/board"


def project_board(slug: str, ws: str, proj: str) -> str:
    return f"/api/v1/orgs/{slug}/workspaces/{ws}/projects/{proj}/board"


def get_board(
    client: TestClient, h: dict[str, str], url: str, params: dict[str, Any] | None = None
) -> Any:  # Any: a BoardOut body
    r = client.get(url, headers=h, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def same_404(r: httpx.Response, missing: httpx.Response) -> None:
    """Refused exactly like a slug that does not exist: no existence hint."""
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


def without_time(body: dict[str, Any]) -> dict[str, Any]:  # Any: a BoardOut body
    return {k: v for k, v in body.items() if k != "generated_at"}


def cells(body: dict[str, Any], solution_id: object, target_id: object) -> Any:  # Any: a target
    for project in body["projects"]:
        for sol in project["solutions"]:
            if sol["id"] == str(solution_id):
                for t in sol["targets"]:
                    if t["target_id"] == str(target_id):
                        return t
    raise AssertionError(f"no cell ({solution_id}, {target_id})")


# ------------------------------------------------------------------ people
def member(
    owner_db: Database,
    tenant: UUID,
    permissions: list[str],
    *,
    scope_type: str = "tenant",
    scope_id: UUID | None = None,
) -> str:
    """A member of `tenant` whose only grant is one custom role with exactly `permissions` at the
    scope; the e-mail (password `harness.PASSWORD`)."""
    email = f"board-{uuid7().hex[-12:]}@route.test"
    with owner_db.session() as o:
        uid = harness.person(o, email)
        owner_member(o, tenant, uid)
        o.commit()
    grant(owner_db, tenant, uid, permissions, scope_type=scope_type, scope_id=scope_id)
    return email


# ------------------------------------------------------------------ rows (schema owner)
def owner_one(owner_db: Database, sql: str, **params: Any) -> Any:  # Any: a Row
    with owner_db.session() as o:
        return o.execute(text(sql), params).one()


def owner_run(owner_db: Database, sql: str, **params: Any) -> None:
    with owner_db.session() as o:
        o.execute(text(sql), params)
        o.commit()


def add_project(
    owner_db: Database, tenant: UUID, workspace_id: UUID, status: str = "active"
) -> UUID:
    pid = uuid7()
    owner_run(
        owner_db,
        "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name, status) "
        "VALUES (:i, :t, :w, :s, :s, :st)",
        i=pid,
        t=tenant,
        w=workspace_id,
        s=f"p-{pid.hex}",
        st=status,
    )
    return pid


@dataclass(frozen=True)
class Sol:
    """A solution with version 1 and its sealed release (`first`, with a passed run)."""

    tenant: UUID
    id: UUID
    entrypoint: UUID  # the published agent version every version of it names
    dataset: UUID
    dataset_digest: str
    first: UUID
    first_digest: str


def solution(owner_db: Database, tenant: UUID, workspace_id: UUID, project_id: UUID) -> Sol:
    rel = make_release(owner_db, tenant, workspace_id, project_id)
    row = owner_one(
        owner_db,
        "SELECT v.solution_id, v.entrypoint_version_id FROM shifde.releases r "
        "JOIN shifde.solution_versions v ON v.id = r.solution_version_id WHERE r.id = :r",
        r=rel.id,
    )
    return Sol(tenant, row[0], row[1], rel.dataset_id, rel.dataset_digest, rel.id, rel.digest)


def evaluation_run(owner_db: Database, sol: Sol, release_id: UUID, status: str) -> UUID:
    run_id = uuid7()
    owner_run(
        owner_db,
        "INSERT INTO shifde.evaluation_runs (tenant_id, id, release_id, release_digest, "
        "dataset_id, dataset_digest, thresholds, status, requested_by) SELECT :t, :i, r.id, "
        "r.digest, :d, :dd, '{}'::jsonb, :s, :t FROM shifde.releases r WHERE r.id = :r",
        t=sol.tenant,
        i=run_id,
        r=release_id,
        d=sol.dataset,
        dd=sol.dataset_digest,
        s=status,
    )
    return run_id


def release(
    owner_db: Database, sol: Sol, version_no: int, *, evaluation: str | None = "passed"
) -> tuple[UUID, str]:
    """Version `version_no` of `sol` and its sealed release (sealed now); (id, digest)."""
    version, release_id = uuid7(), uuid7()
    digest = uuid7().hex + uuid7().hex
    owner_run(
        owner_db,
        "INSERT INTO shifde.solution_versions (tenant_id, id, solution_id, version_no, "
        "entrypoint_kind, entrypoint_version_id, spec, spec_hash, created_by) VALUES "
        "(:t, :v, :s, :n, 'agent', :e, '{}'::jsonb, :h, :t)",
        t=sol.tenant,
        v=version,
        s=sol.id,
        n=version_no,
        e=sol.entrypoint,
        h="b" * 64,
    )
    owner_run(
        owner_db,
        "INSERT INTO shifde.releases (tenant_id, id, solution_version_id, digest, manifest, "
        "compatibility, status, sealed_by) VALUES (:t, :i, :v, :d, CAST(:m AS jsonb), "
        "'{}'::jsonb, 'sealed', :t)",
        t=sol.tenant,
        i=release_id,
        v=version,
        d=digest,
        m=json.dumps({"nonce": str(release_id)}),
    )
    if evaluation:
        evaluation_run(owner_db, sol, release_id, evaluation)
    return release_id, digest


def target(
    owner_db: Database, tenant: UUID, workspace_id: UUID, project_id: UUID | None = None
) -> UUID:
    """An archived target (no platform claim) of the workspace, or of the project if given."""
    target_id = uuid7()
    owner_run(
        owner_db,
        "INSERT INTO shifde.delivery_targets (tenant_id, id, workspace_id, project_id, name, "
        "platform_kind, address, environment, bindings, credential_ref, status, created_by) "
        "VALUES (:t, :g, :w, :p, :n, 'databricks', :a, 'dev', '{}'::jsonb, 'databricks-dev', "
        "'archived', :t)",
        t=tenant,
        g=target_id,
        w=workspace_id,
        p=project_id,
        n=f"target-{target_id.hex[-8:]}",
        a=address(),
    )
    return target_id


def plan(
    owner_db: Database,
    tenant: UUID,
    target_id: UUID,
    release_id: UUID | None,
    status: str = "requested",
    *,
    sealed_age: str = "0 seconds",
) -> UUID:
    """A plan of the release (none if None) on the target; a sealed, approved or expired one was
    sealed `sealed_age` ago (it expires 15 minutes after sealing)."""
    artifact, plan_id = uuid7(), uuid7()
    owner_run(
        owner_db,
        "INSERT INTO shifde.delivery_artifacts (tenant_id, id, target_kind, digest, size, "
        "content, created_by) VALUES (:t, :i, 'databricks.app/v1', :d, 1, :c, :t)",
        t=tenant,
        i=artifact,
        d="e" * 64,
        c=b"\x00",
    )
    sealed = status in ("sealed", "approved", "rejected", "expired")
    owner_run(
        owner_db,
        "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, target_revision, "
        "artifact_id, artifact_digest, application_key, display_name, requested_by, status, "
        "steps, snapshot, adapter_version, toolchain_digest, digest, sealed_at, expires_at, "
        "release_id, release_digest, evaluation_run_id) "
        "SELECT :t, :i, :g, 1, :a, :d, 'board-app', 'Board app', :t, :st, "
        "CASE WHEN :sealed THEN '[]'::jsonb END, CASE WHEN :sealed THEN '{}'::jsonb END, "
        "CASE WHEN :sealed THEN '0.1.0' END, CASE WHEN :sealed THEN :d END, "
        "CASE WHEN :sealed THEN :d END, "
        "CASE WHEN :sealed THEN now() - CAST(:age AS interval) END, "
        "CASE WHEN :sealed THEN now() - CAST(:age AS interval) + interval '15 minutes' END, "
        "r.id, r.digest, (SELECT e.id FROM shifde.evaluation_runs e WHERE e.release_id = r.id "
        "ORDER BY e.id LIMIT 1) "
        "FROM (SELECT 1) one LEFT JOIN shifde.releases r ON r.id = CAST(:r AS uuid)",
        t=tenant,
        i=plan_id,
        g=target_id,
        a=artifact,
        d="e" * 64,
        st=status,
        sealed=sealed,
        age=sealed_age,
        r=release_id,
    )
    return plan_id


def deployment(
    owner_db: Database,
    tenant: UUID,
    plan_id: UUID,
    target_id: UUID,
    status: str = "queued",
    reason: str | None = None,
) -> UUID:
    """A deployment of the plan in `status`; a status past `queued` has `updated_at` set."""
    deployment_id = uuid7()
    owner_run(
        owner_db,
        "INSERT INTO shifde.delivery_deployments (tenant_id, id, plan_id, target_id, "
        "application_key, status, status_reason, updated_at) VALUES (:t, :i, :p, :g, "
        "'board-app', :s, :why, CASE WHEN :s = 'queued' THEN NULL ELSE clock_timestamp() END)",
        t=tenant,
        i=deployment_id,
        p=plan_id,
        g=target_id,
        s=status,
        why=reason,
    )
    return deployment_id
