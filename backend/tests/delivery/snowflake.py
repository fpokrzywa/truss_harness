"""Snowflake delivery test helpers (SP6-3): a test-only Native App artifact, its plan and plan job,
and the plan's approval, deployment and apply job.

No Snowflake release can be sealed before SP6-4 and the plan route refuses Snowflake targets until
SP6-6, so these rows are written here, as `shifde_app` in the organization's context, through the
same repository and `gateway.enqueue_job` calls the plan and approval routes use.
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

TARGET_KIND = "snowflake.native-app/v1"
# A test-only package: the shape of a rendered one (the generator is SP6-5), no SDK template.
FILES = {
    "manifest.yml": b"manifest_version: 1\nversion:\n  name: V1\n  label: SHIFDE V1\n"
    b"artifacts:\n  setup_script: setup.sql\n  default_streamlit: APP_PUBLIC.UI\n",
    "setup.sql": b"CREATE APPLICATION ROLE IF NOT EXISTS APP_USER;\n"
    b"CREATE OR ALTER VERSIONED SCHEMA APP_PUBLIC;\n",
    "ui/environment.yml": b"name: shifde_native_app\nchannels:\n  - snowflake\n",
    "ui/streamlit_app.py": b"import streamlit as st\n\nst.title('SHIFDE')\n",
}


def native_app_artifact() -> tuple[bytes, str]:
    """The package plus a root `release.json`, and the archive's sha256."""
    release = {"manifest": {"target_kind": TARGET_KIND}, "digest": "0" * 64}
    content = create_archive({**FILES, "release.json": canonical(release)})
    return content, hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class SnowflakePlan:
    id: UUID
    artifact_digest: str
    job_id: UUID
    requested_by: UUID


def snowflake_plan(
    app_db: Database, tenant: UUID, target_id: object, application_key: str = "sales-app"
) -> SnowflakePlan:
    """A `requested` plan of the test-only artifact on the Snowflake target, and its plan job."""
    requester = uuid7()
    content, artifact_digest = native_app_artifact()
    artifact_id, plan_id = uuid7(), uuid7()
    with app_session(app_db, tenant, requester) as s:
        target = repository.get_target(s, UUID(str(target_id)), tenant_id=tenant)
        assert target is not None and target["platform_kind"] == "snowflake"
        repository.insert_artifact(
            s,
            {
                "tenant_id": tenant,
                "id": artifact_id,
                "target_kind": TARGET_KIND,
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
                "release_id": None,
                "release_digest": None,
                "evaluation_run_id": None,
            },
        )
        job_id = gateway.enqueue_job(
            s, tenant_id=tenant, kind="plan", plan_id=plan_id, target_id=target["id"]
        )
        s.commit()
    return SnowflakePlan(plan_id, artifact_digest, job_id, requester)


def approve(app_db: Database, tenant: UUID, plan_id: UUID) -> UUID:
    """A second person's approval of the sealed plan: its deployment and `apply` job, as the
    approval route writes them; the deployment's id."""
    approver = uuid7()
    with app_session(app_db, tenant, approver) as s:
        row = repository.get_plan(s, plan_id, tenant_id=tenant, for_update=True)
        assert row is not None and row["status"] == "sealed", row
        repository.insert_approval(
            s,
            {
                "tenant_id": tenant,
                "id": uuid7(),
                "plan_id": plan_id,
                "plan_digest": row["digest"],
                "decided_by": approver,
                "decision": "approve",
                "note": None,
            },
        )
        repository.decide_plan(s, plan_id, "approved", tenant_id=tenant)
        deployment_id = uuid7()
        repository.insert_deployment(
            s,
            {
                "tenant_id": tenant,
                "id": deployment_id,
                "plan_id": plan_id,
                "target_id": row["target_id"],
                "application_key": row["application_key"],
            },
        )
        gateway.enqueue_job(
            s,
            tenant_id=tenant,
            kind="apply",
            plan_id=plan_id,
            deployment_id=deployment_id,
            target_id=row["target_id"],
            deadline=row["expires_at"],
        )
        s.commit()
    return deployment_id
