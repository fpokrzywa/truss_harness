"""Sealed releases for the delivery tests (SP3-4): the delivery test app has only the delivery
routers, so a release, the chain it keys to (a published agent version, a solution, its version,
a dataset) and its evaluation runs are inserted as the schema owner. The packaging and evaluation
tests and the delivery demo cover the real routes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any  # Any: manifest JSON
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.packaging.canonical import digest

TARGET_SCOPE_SQL = (
    "SELECT tenant_id, workspace_id, project_id FROM shifde.delivery_targets WHERE id = :g"
)


@dataclass(frozen=True)
class SealedRelease:
    tenant: UUID
    id: UUID
    digest: str
    manifest: dict[str, Any]
    dataset_id: UUID
    dataset_digest: str
    run_id: UUID | None  # the passed evaluation run, if any


def _project(o: Session, tenant: UUID, workspace_id: UUID) -> UUID:
    """The workspace's first project, made when it has none."""
    found = o.execute(
        text(
            "SELECT id FROM shifde.projects WHERE tenant_id = :t AND workspace_id = :w ORDER BY id"
        ),
        {"t": tenant, "w": workspace_id},
    ).first()
    if found is not None:
        return UUID(str(found[0]))
    project_id = uuid7()
    o.execute(
        text(
            "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
            "VALUES (:i, :t, :w, :s, :s)"
        ),
        {"i": project_id, "t": tenant, "w": workspace_id, "s": f"p-{project_id.hex}"},
    )
    return project_id


def insert_run(owner_db: Database, rel: SealedRelease, status: str, dataset_digest: str) -> UUID:
    """An evaluation run of `rel` with `status` and `dataset_digest`, as the schema owner."""
    run_id = uuid7()
    with owner_db.session() as o:
        set_tenant(o, rel.tenant)
        o.execute(
            text(
                "INSERT INTO shifde.evaluation_runs (tenant_id, id, release_id, release_digest, "
                "dataset_id, dataset_digest, thresholds, status, requested_by) VALUES (:t, :i, "
                ":r, :rd, :d, :dd, CAST(:th AS jsonb), :s, :t)"
            ),
            {
                "t": rel.tenant,
                "i": run_id,
                "r": rel.id,
                "rd": rel.digest,
                "d": rel.dataset_id,
                "dd": dataset_digest,
                "th": json.dumps({"min_accuracy": "0.9"}),
                "s": status,
            },
        )
        o.commit()
    return run_id


def withdraw(owner_db: Database, release_id: object) -> None:
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.releases SET status = 'withdrawn' WHERE id = :r"), {"r": release_id}
        )
        o.commit()


def make_release(
    owner_db: Database,
    tenant: UUID,
    workspace_id: UUID,
    project_id: UUID | None = None,
    *,
    evaluation: str | None = "passed",
    withdrawn: bool = False,
    target_kind: str = "databricks.app/v1",
    extra: dict[str, Any] | None = None,
) -> SealedRelease:
    """A sealed release of `tenant` in `workspace_id` (`project_id`, or the workspace's first
    project), its manifest naming a dataset of that project; with an evaluation run of
    `evaluation` status on the manifest's digests (none if None), withdrawn if asked. `extra`
    entries are added to the manifest."""
    agent, agent_v1, solution, version = uuid7(), uuid7(), uuid7(), uuid7()
    dataset_id, release_id, by = uuid7(), uuid7(), uuid7()
    cases = [{"case_id": "c1", "input": {"message": "hello"}, "assertions": []}]
    dataset_digest = digest(cases)
    with owner_db.session() as o:
        set_tenant(o, tenant)
        project = project_id or _project(o, tenant, workspace_id)
        values = {
            "t": tenant,
            "w": workspace_id,
            "p": project,
            "a": agent,
            "av": agent_v1,
            "s": solution,
            "v": version,
            "d": dataset_id,
            "u": by,
        }
        o.execute(
            text(
                "INSERT INTO shifde.artifacts (id, tenant_id, kind, workspace_id, project_id, key, "
                "name, owner_principal_id) VALUES (:a, :t, 'agents', :w, :p, :k, 'Agent', :u)"
            ),
            {**values, "k": f"agent-{agent.hex[-12:]}"},
        )
        o.execute(
            text(
                "INSERT INTO shifde.artifact_versions (id, tenant_id, artifact_id, kind, "
                "version_no, state, spec_hash, published_at) VALUES (:av, :t, :a, 'agents', 1, "
                "'published', :h, now())"
            ),
            {**values, "h": "a" * 64},
        )
        o.execute(
            text(
                "INSERT INTO shifde.solutions (tenant_id, id, workspace_id, project_id, name, "
                "target_kind, created_by) VALUES (:t, :s, :w, :p, :n, :k, :u)"
            ),
            {**values, "n": f"solution-{solution.hex[-12:]}", "k": target_kind},
        )
        o.execute(
            text(
                "INSERT INTO shifde.solution_versions (tenant_id, id, solution_id, version_no, "
                "entrypoint_kind, entrypoint_version_id, spec, spec_hash, created_by) VALUES "
                "(:t, :v, :s, 1, 'agent', :av, '{}'::jsonb, :h, :u)"
            ),
            {**values, "h": "b" * 64},
        )
        o.execute(
            text(
                "INSERT INTO shifde.evaluation_datasets (tenant_id, id, workspace_id, project_id, "
                "name, version_no, cases, digest, created_by) VALUES (:t, :d, :w, :p, :n, 1, "
                "CAST(:c AS jsonb), :dd, :u)"
            ),
            {
                **values,
                "n": f"golden-{dataset_id.hex[-12:]}",
                "c": json.dumps(cases),
                "dd": dataset_digest,
            },
        )
        manifest: dict[str, Any] = {
            "release_schema": 1,
            "target_kind": target_kind,
            "solution": {"id": str(solution), "version_id": str(version)},
            "entrypoint": {"kind": "agent", "version_id": str(agent_v1)},
            "evaluation": {
                "dataset_id": str(dataset_id),
                "dataset_digest": dataset_digest,
                "min_accuracy": "0.9",
            },
            "nonce": str(release_id),
            **(extra or {}),
        }
        release_digest = digest(manifest)
        o.execute(
            text(
                "INSERT INTO shifde.releases (tenant_id, id, solution_version_id, digest, "
                "manifest, compatibility, status, sealed_by) VALUES (:t, :i, :v, :rd, "
                "CAST(:m AS jsonb), '{}'::jsonb, 'sealed', :u)"
            ),
            {**values, "i": release_id, "rd": release_digest, "m": json.dumps(manifest)},
        )
        o.commit()
    rel = SealedRelease(
        tenant, release_id, release_digest, manifest, dataset_id, dataset_digest, None
    )
    run_id = insert_run(owner_db, rel, evaluation, dataset_digest) if evaluation else None
    if withdrawn:
        withdraw(owner_db, release_id)
    return SealedRelease(
        tenant, release_id, release_digest, manifest, dataset_id, dataset_digest, run_id
    )


def release_for_target(owner_db: Database, target_id: object, **options: Any) -> SealedRelease:
    """A release in the target's workspace and project (`make_release` options)."""
    with owner_db.session() as o:
        tenant, workspace_id, project_id = o.execute(text(TARGET_SCOPE_SQL), {"g": target_id}).one()
    return make_release(owner_db, tenant, workspace_id, project_id, **options)
