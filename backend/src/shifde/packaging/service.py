"""Solutions, solution versions and releases (sub-project 3 design sections 4 and 5, settled calls
7, 8, 11 to 15).

A solution belongs to one project; its routes name the workspace and project in the path, and a
solution (or version) that is not in that project, or that the caller cannot read, gets the
answer an unknown id gets. A solution version pins one published agent or pipeline version the
caller can see; its pins are the transitive closure of the entrypoint's pins (`resolve_pins`,
depth cap 8), read through `shifde.build.service` and resolved again on every read (published
build versions and their pins never change). Each version's `spec_hash` is the sha256 of the
canonical JSON of its spec (`packaging.canonical`). The entrypoint and every data-read connection
must be in the solution's scope (organization-wide, or the solution's workspace and, if any, its
project); a pin of an artifact the caller cannot see is shown without its version id and hash.

Sealing a version runs the `databricks.app/v1` compatibility check over its pinned tools, models,
connections and evaluation contract; a compatible version becomes a release whose `digest` is the
sha256 of its canonical manifest (sealing it again with the same bindings returns that release),
an incompatible one is refused with the whole report (`release_incompatible`). A release's scope
is its solution's; it is reached under the organization (`/orgs/{org}/releases/{id}`) and gets
the answer an unknown id gets when the caller cannot read its solution.

A version's evaluation contract names an evaluation dataset: it must be a dataset of this client,
in the solution's workspace and project, that the caller may read (`evaluation:dataset:read`),
when the version is created (else 422) and when it is sealed (else the `evaluation` diagnostic is
unsupported); an unknown id, another client's, another project's and an unreadable dataset get
one answer (isolation review 029, finding 3; B-2703). The manifest records the dataset's digest.
`packaging` sits below `evaluation`, so the dataset is read by one tenant-keyed SELECT of
`evaluation_datasets` in `packaging.repository` (`get_dataset_placement`), not through
`evaluation`'s service.

Every row is read and written for the caller's resolved client only (`TenantScope.tenant_id`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any  # Any: solution and version rows, JSON specs
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.build.executors import WRITE_CLASSES
from shifde.build.service import ArtifactService, PinnedVersion, VersionAccess
from shifde.connections.service import ConnectionService
from shifde.core.errors import AppError, Conflict, NotFound, ScopeArchived, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.models_admin.service import ModelService
from shifde.packaging import repository
from shifde.packaging import schemas as s
from shifde.packaging.canonical import digest
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

READ, WRITE = "packaging:solution:read", "packaging:solution:write"
SEAL, WITHDRAW = "packaging:release:seal", "packaging:release:withdraw"
MAX_PIN_DEPTH = 8
MAX_SPEC_BYTES = 262_144
ENTRYPOINT_KINDS = {"agent": "agents", "pipeline": "pipelines"}
ENTRYPOINT_REFUSED = "the entrypoint must be a published agent or pipeline version you can see"
CONNECTION_REFUSED = "the connection is not a connection of this organization you can read"
DUPLICATE_NAME = "a solution with this name already exists in the project"
UNKNOWN_BINDING = "the binding names no pin of this solution version"

# the databricks.app/v1 compatibility check (design section 5, settled calls 14 and 15)
COMPILER_VERSION = "shifde.packaging/1"
DATABRICKS = "databricks"
READ_ONLY_TOOL_EXECUTORS = frozenset({"connection.sql"})
READ_ONLY_SIDE_EFFECTS = frozenset({"none", "read"})
OUTSIDE_EXECUTORS = frozenset({"rest.http", "mcp.tool"})
ENTRYPOINT_GONE = "the entrypoint is no longer a published agent or pipeline version"
NEEDS_APPROVAL = "needs a human approval step"
WRITES = "writes"
CALLS_OUTSIDE = "calls an outside service"
NOT_READ_ONLY = "not on the read-only list"
CLIENT_MODELS = "a deployed application uses models served in the client's workspace"
MODEL_ROUTE = "a model route cannot be released; name a model"
NO_MODEL_BINDING = "no model binding"
NOT_A_PROVIDER = "the provider is not a model provider of this organization"
NO_CONNECTION_BINDING = "no connection binding"
NOT_DATABRICKS = (
    "the bound connection is not a Databricks connection of this organization in the solution's "
    "scope"
)
NO_EVALUATION = "a release needs an evaluation dataset"
DATASET_REFUSED = "the evaluation dataset is not a dataset of this project you can read"
DATASET_READ = "evaluation:dataset:read"
HIDDEN_TOOL = "tools.hidden"
Report = list[dict[str, Any]]  # Any: diagnostics (JSON)


class ReleaseIncompatible(AppError):
    status, code, title = 422, "release_incompatible", "Unprocessable Entity"


def in_scope(
    solution: Mapping[str, Any], workspace_id: UUID | None, project_id: UUID | None
) -> bool:
    """An object is in a solution's scope when it is organization-wide, or in the solution's
    workspace and either workspace-wide or in the solution's project."""
    if workspace_id is None:
        return True
    return workspace_id == solution["workspace_id"] and project_id in (
        None,
        solution["project_id"],
    )


@dataclass
class _Pinned:
    """What a solution version names, by pin name: tool versions (with their specs), models
    (`own`, `shared` or `route`) and connections."""

    tools: dict[str, VersionAccess] = field(default_factory=dict)
    tool_specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    connections: set[str] = field(default_factory=set)

    def model(self, name: str, kind: str) -> None:
        if self.models.get(name) != "shared":  # one shared sighting makes it shared
            self.models[name] = kind


def _diagnostic(path: str, reason: str | None) -> dict[str, Any]:
    return {
        "path": path,
        "status": "unsupported" if reason else "supported",
        "reason": reason,
        "mandatory": True,
    }


def _tool_reason(spec: Mapping[str, Any]) -> str | None:
    """None for a read-only tool, else the first reason it is not."""
    policy = spec.get("invocation_policy")
    side = spec.get("side_effect_class")
    executor = spec.get("executor")
    if isinstance(policy, dict) and policy.get("require_approval") is True:
        return NEEDS_APPROVAL
    if side in WRITE_CLASSES:
        return WRITES
    if executor in OUTSIDE_EXECUTORS:
        return CALLS_OUTSIDE
    if executor not in READ_ONLY_TOOL_EXECUTORS or side not in READ_ONLY_SIDE_EFFECTS:
        return NOT_READ_ONLY
    return None


def _uuid(value: Any) -> UUID | None:  # Any: a JSON value
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def release_record(
    session: Session, release_id: UUID, *, tenant_id: UUID
) -> Mapping[str, Any] | None:
    """The release `release_id` of `tenant_id` with its solution's workspace and project, its
    stored (unredacted) manifest and its status; None for an unknown id and another client's
    alike. No permission check: the caller checks its own (evaluation, delivery)."""
    row = repository.get_release(session, release_id, tenant_id=tenant_id)
    return row if row is not None and row["tenant_id"] == tenant_id else None


def scoped_release(scope: TenantScope, release_id: UUID, action: str = READ) -> Mapping[str, Any]:
    """The release the caller may `action` on, its scope its solution's; anything else
    (unknown, another client's, unreadable) is `release not found` (packaging, delivery)."""
    row = release_record(scope.session, release_id, tenant_id=scope.tenant_id)
    if row is None:
        raise NotFound("release not found")
    ref = ResourceRef(
        scope.tenant_id,
        row["workspace_id"],
        row["project_id"],
        resource_type="packaging_release",
        resource_id=row["id"],
    )
    try:
        scope.pdp.require(action, ref, hide_unless=READ)
    except NotFound as exc:
        raise NotFound("release not found") from exc
    return row


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
        self.models = ModelService(scope, runtime)

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
        outcome: str = "success",
    ) -> None:
        audit.record(
            self.sc.session,
            self.sc.ctx,
            action=action,
            outcome=outcome,
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

    def _seen(self, version_id: UUID) -> bool:
        """Whether the caller may see the artifact of a version of this client."""
        access = self.artifacts.version_access(version_id)
        return access is not None and access.visible

    def _version_out(self, row: Mapping[str, Any]) -> s.SolutionVersionOut:
        entry = self.artifacts.version_access(row["entrypoint_version_id"])
        pins = resolve_pins(row["entrypoint_version_id"], self.artifacts.direct_pins)
        shown = entry is not None and entry.visible
        return s.SolutionVersionOut(
            id=row["id"],
            solution_id=row["solution_id"],
            version_no=row["version_no"],
            entrypoint=s.EntrypointOut(
                kind=row["entrypoint_kind"],
                version_id=row["entrypoint_version_id"] if shown else None,
                spec_hash=entry.spec_hash if entry is not None and shown else None,
            ),
            spec=dict(row["spec"]),
            spec_hash=row["spec_hash"],
            pins=[
                s.SolutionPinOut(role=p.role, version_id=p.version_id, spec_hash=p.spec_hash)
                if self._seen(p.version_id)
                else s.SolutionPinOut(role=p.role, version_id=None, spec_hash=None)
                for p in pins
            ],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def _check_entrypoint(
        self,
        solution: Mapping[str, Any],
        version_id: UUID,
        loc: list[Any],  # Any: path parts
        *,
        kind: str | None,
    ) -> VersionAccess:
        """The entrypoint version, visible to the caller and in the solution's scope (with `kind`,
        also a published version of that kind). Unknown, another client's, unseen, out-of-scope,
        wrong-kind and unpublished get one answer."""
        entry = self.artifacts.version_access(version_id)
        if (
            entry is None
            or not entry.visible
            or not in_scope(solution, entry.workspace_id, entry.project_id)
            or (
                kind is not None
                and (entry.kind != ENTRYPOINT_KINDS[kind] or entry.state != "published")
            )
        ):
            raise ValidationFailed(
                ENTRYPOINT_REFUSED,
                errors=[{"loc": loc, "msg": ENTRYPOINT_REFUSED, "type": "entrypoint"}],
            )
        return entry

    def _check_connections(
        self,
        solution: Mapping[str, Any],
        connection_ids: Sequence[UUID],
        loc: list[Any],  # Any: path parts
    ) -> None:
        """Each data-read connection is readable by the caller and in the solution's scope;
        unknown, another client's, unreadable and out-of-scope ids get one answer."""
        errors = []
        for i, connection_id in enumerate(connection_ids):
            placement = self.conns.placement(connection_id)
            if placement is None or not in_scope(
                solution, placement.workspace_id, placement.project_id
            ):
                errors.append(
                    {
                        "loc": [*loc, i, "connection_id"],
                        "msg": CONNECTION_REFUSED,
                        "type": "connection",
                    }
                )
        if errors:
            raise ValidationFailed(CONNECTION_REFUSED, errors=errors)

    def _dataset(
        self, solution: Mapping[str, Any], dataset_id: UUID | None
    ) -> Mapping[str, Any] | None:
        """The evaluation dataset `dataset_id` when it is a dataset of this client in the
        solution's workspace and project that the caller may read; else None (unknown, another
        client's, another project's and unreadable alike)."""
        if dataset_id is None:
            return None
        row = repository.get_dataset_placement(
            self.sc.session, dataset_id, tenant_id=self.sc.tenant_id
        )
        if (
            row is None
            or row["tenant_id"] != self.sc.tenant_id
            or row["workspace_id"] != solution["workspace_id"]
            or row["project_id"] != solution["project_id"]
        ):
            return None
        ref = ResourceRef(
            self.sc.tenant_id,
            row["workspace_id"],
            row["project_id"],
            resource_type="evaluation_dataset",
            resource_id=row["id"],
        )
        return row if self.sc.pdp.authorize(DATASET_READ, ref).allowed else None

    def _check_dataset(self, solution: Mapping[str, Any], body: s.SolutionVersionCreate) -> None:
        evaluation = body.evaluation
        if evaluation is not None and self._dataset(solution, evaluation.dataset_id) is None:
            raise ValidationFailed(
                DATASET_REFUSED,
                errors=[
                    {
                        "loc": ["body", "evaluation", "dataset_id"],
                        "msg": DATASET_REFUSED,
                        "type": "dataset",
                    }
                ],
            )

    def _version_of(self, solution: Mapping[str, Any], version_id: UUID) -> Mapping[str, Any]:
        """The solution's version `version_id`; any other id is `solution version not found`."""
        row = repository.get_version(self.sc.session, version_id, tenant_id=self.sc.tenant_id)
        if (
            row is None
            or row["tenant_id"] != self.sc.tenant_id
            or row["solution_id"] != solution["id"]
        ):
            raise NotFound("solution version not found")
        return row

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
        self._check_entrypoint(
            row,
            body.entrypoint_version_id,
            ["body", "entrypoint_version_id"],
            kind=body.entrypoint_kind,
        )
        self._check_connections(
            row, [r.connection_id for r in body.data_reads], ["body", "data_reads"]
        )
        self._check_dataset(row, body)
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
        solution = self._solution(ws, proj, solution_id, READ)
        return self._version_out(self._version_of(solution, version_id))

    # ================================================================== releases
    def _pinned(self, version: Mapping[str, Any], pins: Sequence[PinnedVersion]) -> _Pinned:
        """The tools, models and connections the version names: its data reads, and every tool,
        agent and pipeline among its entrypoint and transitive pins."""
        found = _Pinned()
        found.connections.update(str(r["connection_id"]) for r in version["spec"]["data_reads"])
        for version_id in dict.fromkeys(
            [version["entrypoint_version_id"], *(p.version_id for p in pins)]
        ):
            access = self.artifacts.version_access(version_id)
            if access is None:
                continue
            if access.kind == "pipelines":
                for ref in self.artifacts.pipeline_resource_refs(version_id):
                    if ref.ref_type == "connection":
                        found.connections.add(str(ref.ref_id))
                    else:
                        found.model(str(ref.ref_id), "own" if ref.own else "shared")
                continue
            spec = self.artifacts.version_spec(version_id) or {}
            if access.kind == "tools":
                found.tools[str(version_id)] = access
                found.tool_specs[str(version_id)] = spec
                binding = spec.get("binding")
                connection_id = (
                    _uuid(binding.get("connection_id")) if isinstance(binding, dict) else None
                )
                if connection_id is not None:
                    found.connections.add(str(connection_id))
            elif access.kind == "agents":
                model = spec.get("model")
                if not isinstance(model, dict):
                    continue
                if "route_id" in model:
                    found.model(f"route-{model['route_id']}", "route")
                    continue
                for value in [model.get("model_id"), *(model.get("fallback_model_ids") or [])]:
                    model_id = _uuid(value)
                    if model_id is not None:
                        own = self.models.own_model(model_id)
                        found.model(str(model_id), "own" if own else "shared")
        return found

    @staticmethod
    def _check_binding_keys(bindings: s.EnvironmentBindings, found: _Pinned) -> None:
        errors = [
            {
                "loc": ["body", "environment_bindings", "model_bindings", key],
                "msg": UNKNOWN_BINDING,
                "type": "binding",
            }
            for key in bindings.model_bindings
            if key not in found.models
        ] + [
            {
                "loc": ["body", "environment_bindings", "connection_bindings", key],
                "msg": UNKNOWN_BINDING,
                "type": "binding",
            }
            for key in bindings.connection_bindings
            if key not in found.connections
        ]
        if errors:
            raise ValidationFailed(UNKNOWN_BINDING, errors=errors)

    def _model_reason(self, kind: str, binding: s.ModelBinding | None) -> str | None:
        if kind == "shared":
            return CLIENT_MODELS
        if kind == "route":
            return MODEL_ROUTE
        if binding is None:
            return NO_MODEL_BINDING
        owner = self.models.provider_owner(binding.provider_id)
        if owner == "shared":
            return CLIENT_MODELS
        if owner is None:
            return NOT_A_PROVIDER
        return None

    def _connection_reason(self, solution: Mapping[str, Any], bound: UUID | None) -> str | None:
        if bound is None:
            return NO_CONNECTION_BINDING
        placement = self.conns.placement(bound)
        if (
            placement is None
            or placement.adapter_key != DATABRICKS
            or not in_scope(solution, placement.workspace_id, placement.project_id)
        ):
            return NOT_DATABRICKS
        return None

    def _report(
        self,
        solution: Mapping[str, Any],
        version: Mapping[str, Any],
        entry: VersionAccess,
        found: _Pinned,
        bindings: s.EnvironmentBindings,
    ) -> Report:
        """One diagnostic per feature: entrypoint, tools, models, connections (each group by pin
        name), evaluation. Every one is mandatory (no accepted deviations, B-2699)."""
        published = (
            entry.state == "published"
            and entry.kind == ENTRYPOINT_KINDS[version["entrypoint_kind"]]
        )
        report = [_diagnostic("entrypoint", None if published else ENTRYPOINT_GONE)]
        report += [
            _diagnostic(f"tools.{name}", _tool_reason(found.tool_specs[name]))
            for name in sorted(found.tools)
        ]
        report += [
            _diagnostic(
                f"models.{name}",
                self._model_reason(found.models[name], bindings.model_bindings.get(name)),
            )
            for name in sorted(found.models)
        ]
        report += [
            _diagnostic(
                f"connections.{name}",
                self._connection_reason(solution, bindings.connection_bindings.get(name)),
            )
            for name in sorted(found.connections)
        ]
        report.append(_diagnostic("evaluation", self._evaluation_reason(solution, version)))
        return report

    def _evaluation_reason(
        self, solution: Mapping[str, Any], version: Mapping[str, Any]
    ) -> str | None:
        evaluation = version["spec"].get("evaluation")
        if not evaluation:
            return NO_EVALUATION
        if self._dataset(solution, _uuid(evaluation.get("dataset_id"))) is None:
            return DATASET_REFUSED
        return None

    @staticmethod
    def _shown_path(path: str, hidden: Callable[[str], bool]) -> str:
        """`tools.<pin>` of a tool version the caller cannot see is `tools.hidden`."""
        kind, _, name = path.partition(".")
        return HIDDEN_TOOL if kind == "tools" and hidden(name) else path

    def _hidden_tool(self, name: str) -> bool:
        version_id = _uuid(name)
        return version_id is None or not self._seen(version_id)

    def _manifest(
        self,
        solution: Mapping[str, Any],
        version: Mapping[str, Any],
        entry: VersionAccess,
        pins: Sequence[PinnedVersion],
        bindings: s.EnvironmentBindings,
    ) -> dict[str, Any]:
        """The release manifest: no digest, sealing time or sealer (settled call 12)."""
        evaluation = version["spec"]["evaluation"]
        dataset = self._dataset(solution, _uuid(evaluation["dataset_id"]))
        assert dataset is not None  # the report's `evaluation` diagnostic was supported
        return {
            "schema_version": 1,
            "solution_id": str(solution["id"]),
            "solution_version_id": str(version["id"]),
            "version_no": version["version_no"],
            "target_kind": solution["target_kind"],
            "entrypoint": {
                "kind": version["entrypoint_kind"],
                "version_id": str(entry.version_id),
                "spec_hash": entry.spec_hash,
            },
            "pins": [
                {"role": p.role, "version_id": str(p.version_id), "spec_hash": p.spec_hash}
                for p in pins
            ],
            "data_reads": version["spec"]["data_reads"],
            "bindings": {
                "model_bindings": {
                    k: {"provider_id": str(v.provider_id), "model": v.model}
                    for k, v in bindings.model_bindings.items()
                },
                "connection_bindings": {k: str(v) for k, v in bindings.connection_bindings.items()},
            },
            "evaluation": {
                "dataset_id": evaluation["dataset_id"],
                "dataset_digest": dataset["digest"],
                "thresholds": {
                    "min_accuracy": evaluation["min_accuracy"],
                    "max_p95_latency_ms": evaluation["max_p95_latency_ms"],
                    "max_total_cost_usd": evaluation["max_total_cost_usd"],
                },
            },
            "compiler_version": COMPILER_VERSION,
        }

    def _release_out(self, row: Mapping[str, Any]) -> s.ReleaseOut:
        """The release; the manifest's entrypoint and pins of versions the caller cannot see lose
        their version id and hash, and their hidden tool paths read `tools.hidden`."""
        manifest = json.loads(json.dumps(row["manifest"]))
        for item in [manifest["entrypoint"], *manifest["pins"]]:
            version_id = _uuid(item["version_id"])
            if version_id is None or not self._seen(version_id):
                item["version_id"] = item["spec_hash"] = None
        compatibility = [
            s.Diagnostic(**{**d, "path": self._shown_path(d["path"], self._hidden_tool)})
            for d in row["compatibility"]
        ]
        workspaces, projects = tenancy.scope_slug_map(
            self.sc, [row["workspace_id"]], [row["project_id"]]
        )
        return s.ReleaseOut(
            id=row["id"],
            solution_id=row["solution_id"],
            solution_version_id=row["solution_version_id"],
            version_no=row["version_no"],
            workspace=workspaces.get(row["workspace_id"]),
            project=projects.get(row["project_id"]),
            digest=row["digest"],
            status=row["status"],
            manifest=manifest,
            compatibility=compatibility,
            sealed_by=row["sealed_by"],
            sealed_at=row["sealed_at"],
            updated_at=row["updated_at"],
        )

    def seal(
        self, ws: str, proj: str, solution_id: UUID, version_id: UUID, body: s.ReleaseCreate
    ) -> tuple[s.ReleaseOut, bool]:
        """(the release, whether it is new): a new release, or the existing one of the same
        digest; an incompatible version is refused with the whole report."""
        row = self._solution(ws, proj, solution_id, SEAL, lock=True)
        version = self._version_of(row, version_id)
        if row["status"] != "active":
            raise Conflict("the solution is archived")
        self._active_scope(ws, proj)
        entry = self._check_entrypoint(
            row, version["entrypoint_version_id"], ["path", "version_id"], kind=None
        )
        self._check_connections(
            row,
            [UUID(str(r["connection_id"])) for r in version["spec"]["data_reads"]],
            ["spec", "data_reads"],
        )
        pins = resolve_pins(version["entrypoint_version_id"], self.artifacts.direct_pins)
        found = self._pinned(version, pins)
        bindings = body.environment_bindings
        self._check_binding_keys(bindings, found)
        report = self._report(row, version, entry, found, bindings)
        session = self.sc.session
        if any(d["status"] == "unsupported" for d in report):
            hidden = {name for name, access in found.tools.items() if not access.visible}

            def shown(path: str) -> str:
                return self._shown_path(path, hidden.__contains__)

            self._audit(
                "packaging.release.refused",
                row,
                {
                    "solution_version_id": str(version["id"]),
                    "version_no": version["version_no"],
                    "unsupported": [
                        shown(d["path"]) for d in report if d["status"] == "unsupported"
                    ],
                },
                target_type="packaging_solution_version",
                target_id=version["id"],
                outcome="failure",
            )
            session.commit()
            errors = []
            for d in report:
                path = shown(d["path"])
                errors.append(
                    {
                        "loc": ["compatibility", *path.split(".")],
                        "msg": d["reason"] or "supported",
                        "type": d["status"],
                        **d,
                        "path": path,
                    }
                )
            raise ReleaseIncompatible(
                "The release is not compatible with its target.", errors=errors
            )
        manifest = self._manifest(row, version, entry, pins, bindings)
        release_digest = digest(manifest)
        release_id = repository.insert_release(
            session,
            {
                "id": uuid7(),
                "tenant_id": self.sc.tenant_id,
                "solution_version_id": version["id"],
                "digest": release_digest,
                "manifest": manifest,
                "compatibility": report,
                "sealed_by": self.sc.principal.principal_id,
            },
        )
        if release_id is None:  # sealed before: the existing release, unchanged and unaudited
            existing = repository.get_release_by_digest(
                session, release_digest, tenant_id=self.sc.tenant_id
            )
            session.rollback()
            assert existing is not None
            return self._release_out(existing), False
        self._audit(
            "packaging.release.sealed",
            row,
            {
                "digest": release_digest,
                "solution_version_id": str(version["id"]),
                "version_no": version["version_no"],
            },
            target_type="packaging_release",
            target_id=release_id,
        )
        session.commit()
        return self._release_out(self._release(release_id, READ)), True

    def list_releases(
        self,
        ws: str,
        proj: str,
        solution_id: UUID,
        version_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> Page[s.ReleaseOut]:
        version = self._version_of(self._solution(ws, proj, solution_id, READ), version_id)
        rows = repository.list_releases(
            self.sc.session,
            version["id"],
            tenant_id=self.sc.tenant_id,
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._release_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def _release(self, release_id: UUID, action: str) -> Mapping[str, Any]:
        """The release the caller may `action` on (`scoped_release`)."""
        return scoped_release(self.sc, release_id, action)

    def get_release(self, release_id: UUID) -> s.ReleaseOut:
        return self._release_out(self._release(release_id, READ))

    def withdraw(self, release_id: UUID, body: s.ReleaseWithdraw) -> s.ReleaseOut:
        row = self._release(release_id, WITHDRAW)
        session = self.sc.session
        if not repository.withdraw_release(session, release_id, tenant_id=self.sc.tenant_id):
            session.rollback()  # already withdrawn, or by a concurrent request
            raise Conflict("the release is already withdrawn")
        self._audit(
            "packaging.release.withdrawn",
            {
                "id": row["solution_id"],
                "name": row["solution_name"],
                "workspace_id": row["workspace_id"],
                "project_id": row["project_id"],
            },
            {"digest": row["digest"], "note": body.note},
            target_type="packaging_release",
            target_id=release_id,
        )
        session.commit()
        return self._release_out(self._release(release_id, READ))
