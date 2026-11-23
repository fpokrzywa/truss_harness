"""What the spec validators need from the service layer (a Protocol, so the validators stay pure
and unit-testable)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol  # Any: stored JSON specs
from uuid import UUID


@dataclass(frozen=True)
class VersionRef:
    """A version row of an artifact that the publisher may see (else the lookup returns None)."""

    version_id: UUID
    artifact_id: UUID
    kind: str
    version_no: int
    state: str
    spec: dict[str, Any]
    spec_hash: str | None
    artifact_key: str
    artifact_name: str
    artifact_status: str
    workspace_id: UUID | None = None  # owning scope of the artifact (runtime resource refs)
    project_id: UUID | None = None


@dataclass(frozen=True)
class ConnectionInfo:
    id: UUID
    connection_type: str
    adapter_key: str
    status: str
    access_mode: str
    usable_by_publisher: bool
    implemented: bool
    capabilities: frozenset[str] = frozenset()  # the adapter's declared capabilities


@dataclass(frozen=True)
class McpDiscovery:
    exists: bool
    tool: dict[str, Any] | None  # {name, description?, input_schema} when the tool was discovered


class Env(Protocol):
    # False when re-validating on behalf of an approver: messages must not name artifacts
    reveal_names: bool

    def version(self, version_id: UUID) -> VersionRef | None: ...

    def latest(self, artifact_id: UUID) -> VersionRef | None: ...

    def model_problems(self, model_ids: list[UUID]) -> dict[UUID, str]: ...

    def route_model_ids(self, route_id: UUID) -> list[UUID] | None: ...

    def connection(self, connection_id: UUID) -> ConnectionInfo | None: ...

    def mcp_tool(self, connection_id: UUID, tool_name: str) -> McpDiscovery: ...

    def schema_discovery(self, connection_id: UUID) -> dict[str, Any] | None:
        """The payload of the connection's latest schema discovery (None: never discovered)."""
        ...

    def executor_maturity(self, executor: str) -> str: ...

    def adapter_maturity(self, adapter_key: str, capability: str) -> str:
        """The derived maturity of an adapter's capability (a `connection.sql` tool on a
        data_platform connection inherits its platform adapter's `query.sql`)."""
        ...

    def permits(self, action: str, ref: VersionRef) -> bool:
        """May the validating principal `action` the artifact of `ref` (execute / invoke / read)?"""
        ...
