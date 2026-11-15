"""A repository-backed `Env` for the build validator tests: every lookup goes through
`build.repository` with the caller's client id, as the service's own `Env` must."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build import repository as repo
from shifde.build.env import VersionRef
from shifde.core.ids import uuid7


class RepoEnv:
    """Only `version` is implemented; the validators under test call nothing else."""

    reveal_names = True

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def version(self, version_id: UUID) -> VersionRef | None:
        v = repo.get_version(self.session, self.tenant_id, version_id)
        if v is None:
            return None
        a = repo.get_artifact(self.session, self.tenant_id, v["artifact_id"])
        if a is None:
            return None
        return VersionRef(
            version_id=v["id"],
            artifact_id=v["artifact_id"],
            kind=v["kind"],
            version_no=v["version_no"],
            state=v["state"],
            spec=v["spec"],
            spec_hash=v["spec_hash"],
            artifact_key=a["key"],
            artifact_name=a["name"],
            artifact_status=a["status"],
        )

    def model_problems(self, model_ids: list[UUID]) -> dict[UUID, str]:
        return {}


def add_published(
    s: Session, tenant: UUID, kind: str, key: str, spec: dict[str, Any]
) -> tuple[UUID, UUID]:
    """(artifact id, published version id), written as the schema owner."""
    artifact, version = uuid7(), uuid7()
    repo.insert_artifact(
        s,
        {
            "id": artifact,
            "tenant_id": tenant,
            "kind": kind,
            "workspace_id": None,
            "project_id": None,
            "key": key,
            "name": key,
            "description": None,
            "owner_principal_id": uuid7(),
            "visibility": "scope",
            "created_by": None,
        },
    )
    repo.insert_version(
        s,
        {
            "id": version,
            "tenant_id": tenant,
            "artifact_id": artifact,
            "kind": kind,
            "version_no": 1,
            "spec": spec,
            "created_by": None,
            "based_on_version_id": None,
        },
    )
    s.execute(
        text(
            "UPDATE shifde.artifact_versions SET state = 'published', spec_hash = 'h', "
            "published_at = now() WHERE id = :i"
        ),
        {"i": version},
    )
    return artifact, version
