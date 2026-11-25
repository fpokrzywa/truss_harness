"""Fabric, Palantir and Docker delivery test helpers (SP8-1): a test-only artifact of each target
kind, its plan and its plan job.

No Fabric release can be planned before SP8-5 (the plan route refuses its target with
`target_kind_mismatch`), a Palantir release is planned through the route (`tests.delivery.palantir`)
and a plan here names no release, so these rows are written here, as `shifde_app` in the
organization's context, through the same repository and `gateway.enqueue_job` calls the plan route
uses (the pattern of `tests.delivery.snowflake`). Plans of Docker releases are
`tests.delivery.docker`'s (SP8-9).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from shifde.adapters.native.base import create_archive
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery import gateway, repository
from shifde.packaging.canonical import canonical
from tests.tenancy.conftest import app_session

TARGET_KINDS = {
    "fabric": "fabric.solution/v1",
    "palantir": "palantir.marketplace/v1",
    "docker": "docker.compose/v1",
}
# One test-only archive per kind: the rough shape of a rendered one.
FILES = {
    "fabric": {
        "fabric/lakehouse.json": b'{"description": "SHIFDE"}\n',
        "fabric/notebook.py": b'mssparkutils.notebook.exit("' + b"0" * 64 + b'")\n',
    },
    "palantir": {"deployment.json": b'{"kind": "palantir.marketplace/v1"}\n'},
    "docker": {
        "Dockerfile": b"FROM python:3.13-slim\n",
        "compose.yaml": b"services:\n  app:\n    build: .\n",
        "app/app.py": b"app = None\n",
    },
}


def sp8_artifact(kind: str) -> tuple[bytes, str]:
    """The archive of `kind` plus a root `release.json`, and the archive's sha256."""
    release = {"manifest": {"target_kind": TARGET_KINDS[kind]}, "digest": "0" * 64}
    content = create_archive({**FILES[kind], "release.json": canonical(release)})
    return content, hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class Sp8Plan:
    id: UUID
    artifact_digest: str
    job_id: UUID
    requested_by: UUID


def sp8_plan(
    app_db: Database, tenant: UUID, target_id: object, application_key: str = "sales-app"
) -> Sp8Plan:
    """A `requested` plan of the test-only artifact of the target's kind, and its plan job."""
    requester = uuid7()
    artifact_id, plan_id = uuid7(), uuid7()
    with app_session(app_db, tenant, requester) as s:
        target = repository.get_target(s, UUID(str(target_id)), tenant_id=tenant)
        assert target is not None and target["platform_kind"] in TARGET_KINDS
        content, artifact_digest = sp8_artifact(target["platform_kind"])
        repository.insert_artifact(
            s,
            {
                "tenant_id": tenant,
                "id": artifact_id,
                "target_kind": TARGET_KINDS[target["platform_kind"]],
                "digest": artifact_digest,
                "size": len(content),
                "content": content,
                "created_by": requester,
            },
        )
        repository.insert_plan(
            s,
            {
                "tenant_id": tenant,
                "id": plan_id,
                "target_id": target["id"],
                "target_revision": target["revision"],
                "artifact_id": artifact_id,
                "artifact_digest": artifact_digest,
                "application_key": application_key,
                "display_name": "Sales app",
                "requested_by": requester,
                "requested_api_key_id": None,
                "release_id": None,
                "release_digest": None,
                "evaluation_run_id": None,
            },
        )
        job_id = gateway.enqueue_job(
            s, tenant_id=tenant, kind="plan", plan_id=plan_id, target_id=target["id"]
        )
        s.commit()
    return Sp8Plan(plan_id, artifact_digest, job_id, requester)
