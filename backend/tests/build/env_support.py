"""A repository-backed `Env` for the build validator tests: every lookup goes through
`build.repository` with the caller's client id, as the service's own `Env` must."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build import repository as repo
from shifde.build.env import ConnectionInfo, Env, McpDiscovery, VersionRef
from shifde.connections import repository as conn_repo
from shifde.core.ids import uuid7
from shifde.models_admin import repository as models_repo


class RepoEnv:
    """The lookups the validators make, each through a repository with the caller's client id (as
    the service's own `Env` must). Models: only the client's own (no SHI switch here)."""

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
            workspace_id=a["workspace_id"],
            project_id=a["project_id"],
        )

    def latest(self, artifact_id: UUID) -> VersionRef | None:
        a = repo.get_artifact(self.session, self.tenant_id, artifact_id)
        if a is None or a["latest_published_version_id"] is None:
            return None
        return self.version(a["latest_published_version_id"])

    def model_problems(self, model_ids: list[UUID]) -> dict[UUID, str]:
        rows = models_repo.get_models(
            self.session, model_ids, tenant_id=self.tenant_id, shared=False
        )
        found = {r["id"]: r for r in rows}
        return {
            m: ("model_not_found" if m not in found else "model_not_active")
            for m in model_ids
            if m not in found or found[m]["lifecycle"] != "active"
        }

    def route_model_ids(self, route_id: UUID) -> list[UUID] | None:
        route = models_repo.get_route(self.session, route_id, tenant_id=self.tenant_id)
        if route is None:
            return None
        return [route["primary_model_id"], *route["fallback_model_ids"]]

    def connection(self, connection_id: UUID) -> ConnectionInfo | None:
        row = conn_repo.get_connection(self.session, connection_id, tenant_id=self.tenant_id)
        if row is None:
            return None
        return ConnectionInfo(
            id=row["id"],
            connection_type=row["connection_type"],
            adapter_key=row["adapter_key"],
            status=row["status"],
            access_mode=row["access_mode"],
            usable_by_publisher=True,
            implemented=True,
        )

    def mcp_tool(self, connection_id: UUID, tool_name: str) -> McpDiscovery:
        found = conn_repo.latest_discovery(self.session, connection_id, tenant_id=self.tenant_id)
        if found is None:
            return McpDiscovery(exists=False, tool=None)
        for tool in found["payload"].get("tools", []):
            if tool["name"] == tool_name:
                return McpDiscovery(exists=True, tool=tool)
        return McpDiscovery(exists=True, tool=None)

    def schema_discovery(self, connection_id: UUID) -> dict[str, Any] | None:
        found = conn_repo.latest_discovery(self.session, connection_id, tenant_id=self.tenant_id)
        return None if found is None else dict(found["payload"])

    def executor_maturity(self, executor: str) -> str:
        return "ready"

    def permits(self, action: str, ref: VersionRef) -> bool:
        """The principal's own permission is the service's decision. What this env decides is that
        `ref` names an artifact of the caller's client, in the scope `ref` claims: the artifact is
        read again with the client id and its workspace and project must be the ones `ref` carries
        (a forged or foreign `VersionRef` gets False)."""
        a = repo.get_artifact(self.session, self.tenant_id, ref.artifact_id)
        return (
            a is not None
            and a["workspace_id"] == ref.workspace_id
            and a["project_id"] == ref.project_id
        )


class Overriding:
    """An `Env` answering as `inner` except for an overridden `version` (a test of what the
    validators do should a lookup ever leak another client's object)."""

    def __init__(
        self, inner: Env, *, version: Callable[[UUID], VersionRef | None] | None = None
    ) -> None:
        self.inner = inner
        self.reveal_names = inner.reveal_names
        self._version = version or inner.version

    def version(self, version_id: UUID) -> VersionRef | None:
        return self._version(version_id)

    def latest(self, artifact_id: UUID) -> VersionRef | None:
        return self.inner.latest(artifact_id)

    def model_problems(self, model_ids: list[UUID]) -> dict[UUID, str]:
        return self.inner.model_problems(model_ids)

    def route_model_ids(self, route_id: UUID) -> list[UUID] | None:
        return self.inner.route_model_ids(route_id)

    def connection(self, connection_id: UUID) -> ConnectionInfo | None:
        return self.inner.connection(connection_id)

    def mcp_tool(self, connection_id: UUID, tool_name: str) -> McpDiscovery:
        return self.inner.mcp_tool(connection_id, tool_name)

    def schema_discovery(self, connection_id: UUID) -> dict[str, Any] | None:
        return self.inner.schema_discovery(connection_id)

    def executor_maturity(self, executor: str) -> str:
        return self.inner.executor_maturity(executor)

    def permits(self, action: str, ref: VersionRef) -> bool:
        return self.inner.permits(action, ref)


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
