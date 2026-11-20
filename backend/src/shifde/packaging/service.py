"""Solutions and solution versions (sub-project 3 design sections 4 and 5, settled calls 7, 8, 11
and 12).

A solution belongs to one project; its routes name the workspace and project in the path, and a
solution (or version) that is not in that project, or that the caller cannot read, gets the
answer an unknown id gets. A solution version pins one published agent or pipeline version the
caller can see; its pins are the transitive closure of the entrypoint's pins (`resolve_pins`,
depth cap 8), read through `shifde.build.service` and resolved again on every read (published
build versions and their pins never change). Each version's `spec_hash` is the sha256 of the
canonical JSON of its spec (`packaging.canonical`).

Every row is read and written for the caller's resolved client only (`TenantScope.tenant_id`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any  # Any: solution and version rows, JSON specs
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.build.service import ArtifactService, PinnedVersion
from shifde.connections.service import ConnectionService
from shifde.core.errors import Conflict, NotFound, ScopeArchived, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.packaging import repository
from shifde.packaging import schemas as s
from shifde.packaging.canonical import digest
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

READ, WRITE = "packaging:solution:read", "packaging:solution:write"
MAX_PIN_DEPTH = 8
MAX_SPEC_BYTES = 262_144
ENTRYPOINT_KINDS = {"agent": "agents", "pipeline": "pipelines"}
ENTRYPOINT_REFUSED = "the entrypoint must be a published agent or pipeline version you can see"
CONNECTION_REFUSED = "the connection is not a connection of this organization you can read"
DUPLICATE_NAME = "a solution with this name already exists in the project"


def resolve_pins(
    entrypoint_version_id: UUID, direct_pins: Callable[[UUID], Sequence[PinnedVersion]]
) -> list[PinnedVersion]:
    """The transitive closure of the entrypoint's pins, each (role, version id) once, sorted by
    (role, version id), without the entrypoint itself. Every version is expanded once (a cycle
    ends); a version at depth 8 that still pins anything is refused."""
    found: dict[tuple[str, UUID], PinnedVersion] = {}
    seen = {entrypoint_version_id}
    frontier = [entrypoint_version_id]
    depth = 0
    while frontier:
        following: list[UUID] = []
        for version_id in frontier:
            pins = direct_pins(version_id)
            if pins and depth == MAX_PIN_DEPTH:
                raise ValidationFailed(f"pins nest deeper than {MAX_PIN_DEPTH} levels")
            for pin in pins:
                if pin.version_id != entrypoint_version_id:
                    found.setdefault((pin.role, pin.version_id), pin)
                if pin.version_id not in seen:
                    seen.add(pin.version_id)
                    following.append(pin.version_id)
        frontier = following
        depth += 1
    return [found[key] for key in sorted(found)]


class SolutionService:
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.artifacts = ArtifactService(scope, runtime)
        self.conns = ConnectionService(scope, runtime)

    # ================================================================== helpers
    def _ref(self, row: Mapping[str, Any]) -> ResourceRef:
        return ResourceRef(
            self.sc.tenant_id,
            row["workspace_id"],
            row["project_id"],
            resource_type="packaging_solution",
            resource_id=row["id"],
        )

    def _project(self, ws: str, proj: str) -> ResourceRef:
        return tenancy.scope_ref_from_slugs(self.sc, ws, proj)

    def _solution(
        self, ws: str, proj: str, solution_id: UUID, action: str, *, lock: bool = False
    ) -> Mapping[str, Any]:
        """The solution of the path's project the caller may `action` on; anything else (unknown,
        another project's or client's, unreadable) is `solution not found`."""
        project = self._project(ws, proj)
        row = repository.get_solution(
            self.sc.session, solution_id, tenant_id=self.sc.tenant_id, lock=lock
        )
        if (
            row is None
            or row["tenant_id"] != self.sc.tenant_id
            or row["workspace_id"] != project.workspace_id
            or row["project_id"] != project.project_id
        ):
            raise NotFound("solution not found")
        try:
            self.sc.pdp.require(action, self._ref(row), hide_unless=READ)
        except NotFound as exc:
            raise NotFound("solution not found") from exc
        return row

    def _active_scope(self, ws: str, proj: str) -> None:
        """Archived organizations, workspaces and projects take no new solution or version."""
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")
        if tenancy.get_workspace(self.sc, ws).status != "active":
            raise ScopeArchived("workspace is archived")
        if tenancy.get_project(self.sc, ws, proj).status != "active":
            raise ScopeArchived("project is archived")

    def _audit(
        self,
        action: str,
        row: Mapping[str, Any],
        details: dict[str, object],
        *,
        target_type: str = "packaging_solution",
        target_id: UUID | None = None,
    ) -> None:
        audit.record(
            self.sc.session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type=target_type,
            target_id=target_id or row["id"],
            target_label=f"solution:{row['name']}",
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details={"name": row["name"], **details},
        )

    @staticmethod
    def _out(row: Mapping[str, Any], ws: str, proj: str) -> s.SolutionOut:
        return s.SolutionOut(
            id=row["id"],
            workspace=ws,
            project=proj,
            name=row["name"],
            target_kind=row["target_kind"],
            status=row["status"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _version_out(self, row: Mapping[str, Any]) -> s.SolutionVersionOut:
        entry = self.artifacts.version_access(row["entrypoint_version_id"])
        pins = resolve_pins(row["entrypoint_version_id"], self.artifacts.direct_pins)
        return s.SolutionVersionOut(
            id=row["id"],
            solution_id=row["solution_id"],
            version_no=row["version_no"],
            entrypoint=s.EntrypointOut(
                kind=row["entrypoint_kind"],
                version_id=row["entrypoint_version_id"],
                spec_hash=entry.spec_hash if entry else None,
            ),
            spec=dict(row["spec"]),
            spec_hash=row["spec_hash"],
            pins=[
                s.PinOut(role=p.role, version_id=p.version_id, spec_hash=p.spec_hash) for p in pins
            ],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def _check_entrypoint(self, body: s.SolutionVersionCreate) -> None:
        """Unknown, another client's, unseen, wrong-kind and unpublished get one answer."""
        entry = self.artifacts.version_access(body.entrypoint_version_id)
        if (
            entry is None
            or not entry.visible
            or entry.kind != ENTRYPOINT_KINDS[body.entrypoint_kind]
            or entry.state != "published"
        ):
            raise ValidationFailed(
                ENTRYPOINT_REFUSED,
                errors=[
                    {
                        "loc": ["body", "entrypoint_version_id"],
                        "msg": ENTRYPOINT_REFUSED,
                        "type": "entrypoint",
                    }
                ],
            )

    def _check_connections(self, body: s.SolutionVersionCreate) -> None:
        errors = [
            {
                "loc": ["body", "data_reads", i, "connection_id"],
                "msg": CONNECTION_REFUSED,
                "type": "connection",
            }
            for i, read in enumerate(body.data_reads)
            if not self.conns.readable(read.connection_id)
        ]
        if errors:
            raise ValidationFailed(CONNECTION_REFUSED, errors=errors)

    @staticmethod
    def _spec(body: s.SolutionVersionCreate) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "data_reads": [
                {"connection_id": str(r.connection_id), "tables": list(r.tables)}
                for r in body.data_reads
            ],
            "screens": [],
            "evaluation": (
                {
                    "dataset_id": str(body.evaluation.dataset_id),
                    "min_accuracy": body.evaluation.min_accuracy,
                    "max_p95_latency_ms": body.evaluation.max_p95_latency_ms,
                    "max_total_cost_usd": body.evaluation.max_total_cost_usd,
                }
                if body.evaluation
                else None
            ),
        }
        # the size the database measures (`octet_length(spec::text)`, jsonb's text form)
        if len(json.dumps(spec, ensure_ascii=False).encode("utf-8")) > MAX_SPEC_BYTES:
            raise ValidationFailed(f"the version spec exceeds {MAX_SPEC_BYTES} bytes")
        return spec

    # ================================================================== solutions
    def list(
        self, ws: str, proj: str, *, status: str | None, limit: int, cursor: str | None
    ) -> Page[s.SolutionOut]:
        project = self._project(ws, proj)
        self.sc.pdp.require(READ, project)
        assert project.project_id is not None
        rows = repository.list_solutions(
            self.sc.session,
            tenant_id=self.sc.tenant_id,
            project_id=project.project_id,
            status=status,
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._out(r, ws, proj) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def create(self, ws: str, proj: str, body: s.SolutionCreate) -> s.SolutionOut:
        project = self._project(ws, proj)
        self.sc.pdp.require(WRITE, project)
        self._active_scope(ws, proj)
        assert project.project_id is not None
        session = self.sc.session
        if repository.name_taken(
            session, project.project_id, body.name, tenant_id=self.sc.tenant_id
        ):
            raise Conflict(DUPLICATE_NAME)
        solution_id = uuid7()
        repository.insert_solution(
            session,
            {
                "id": solution_id,
                "tenant_id": self.sc.tenant_id,
                "workspace_id": project.workspace_id,
                "project_id": project.project_id,
                "name": body.name,
                "target_kind": body.target_kind,
                "created_by": self.sc.principal.principal_id,
            },
        )
        row = repository.get_solution(session, solution_id, tenant_id=self.sc.tenant_id)
        assert row is not None
        self._audit("packaging.solution.created", row, {"target_kind": row["target_kind"]})
        try:
            session.commit()
        except IntegrityError as exc:  # a concurrent create of the same name
            session.rollback()
            raise Conflict(DUPLICATE_NAME) from exc
        return self._out(row, ws, proj)

    def get(self, ws: str, proj: str, solution_id: UUID) -> s.SolutionOut:
        return self._out(self._solution(ws, proj, solution_id, READ), ws, proj)

    def archive(self, ws: str, proj: str, solution_id: UUID) -> s.SolutionOut:
        row = self._solution(ws, proj, solution_id, WRITE)
        if row["status"] != "active":
            raise Conflict("the solution is already archived")
        session = self.sc.session
        archived = repository.archive_solution(session, solution_id, tenant_id=self.sc.tenant_id)
        if archived is None:  # archived by a concurrent request after the read
            session.rollback()
            raise Conflict("the solution is already archived")
        self._audit("packaging.solution.archived", archived, {})
        session.commit()
        return self._out(archived, ws, proj)

    # ================================================================== versions
    def list_versions(
        self, ws: str, proj: str, solution_id: UUID, *, limit: int, cursor: str | None
    ) -> Page[s.SolutionVersionOut]:
        self._solution(ws, proj, solution_id, READ)
        rows = repository.list_versions(
            self.sc.session,
            solution_id,
            tenant_id=self.sc.tenant_id,
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._version_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def create_version(
        self, ws: str, proj: str, solution_id: UUID, body: s.SolutionVersionCreate
    ) -> s.SolutionVersionOut:
        # the solution row lock serializes version numbering
        row = self._solution(ws, proj, solution_id, WRITE, lock=True)
        if row["status"] != "active":
            raise Conflict("the solution is archived")
        self._active_scope(ws, proj)
        self._check_entrypoint(body)
        self._check_connections(body)
        resolve_pins(body.entrypoint_version_id, self.artifacts.direct_pins)  # depth cap
        spec = self._spec(body)
        spec_hash = digest(spec)
        session = self.sc.session
        version_id = uuid7()
        version_no = repository.next_version_no(session, solution_id, tenant_id=self.sc.tenant_id)
        repository.insert_version(
            session,
            {
                "id": version_id,
                "tenant_id": self.sc.tenant_id,
                "solution_id": solution_id,
                "version_no": version_no,
                "entrypoint_kind": body.entrypoint_kind,
                "entrypoint_version_id": body.entrypoint_version_id,
                "spec": spec,
                "spec_hash": spec_hash,
                "created_by": self.sc.principal.principal_id,
            },
        )
        version = repository.get_version(session, version_id, tenant_id=self.sc.tenant_id)
        assert version is not None
        self._audit(
            "packaging.solution_version.created",
            row,
            {
                "version_no": version_no,
                "entrypoint_kind": body.entrypoint_kind,
                "entrypoint_version_id": str(body.entrypoint_version_id),
                "spec_hash": spec_hash,
            },
            target_type="packaging_solution_version",
            target_id=version_id,
        )
        session.commit()
        return self._version_out(version)

    def get_version(
        self, ws: str, proj: str, solution_id: UUID, version_id: UUID
    ) -> s.SolutionVersionOut:
        self._solution(ws, proj, solution_id, READ)
        row = repository.get_version(self.sc.session, version_id, tenant_id=self.sc.tenant_id)
        if (
            row is None
            or row["tenant_id"] != self.sc.tenant_id
            or row["solution_id"] != solution_id
        ):
            raise NotFound("solution version not found")
        return self._version_out(row)
