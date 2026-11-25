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
project); a pin of an artifact the caller cannot see is shown without its version id and hash, and
a data-read connection the caller cannot read without its id (isolation review 065, finding 1).

Sealing a version runs its solution's target kind's compatibility check (`databricks.app/v1` or
`snowflake.native-app/v1`) over its pinned tools, models, connections and evaluation contract; a
compatible version becomes a release whose `digest` is the sha256 of its canonical manifest
(sealing it again with the same bindings returns that release), an incompatible one is refused with
the whole report (`release_incompatible`). A release's scope is its solution's; it is reached under
the organization (`/orgs/{org}/releases/{id}`) and gets the answer an unknown id gets when the
caller cannot read its solution.

The compiler is chosen from the version's `spec.screens` (SP5 design 6.2). A version without
screens is compiled by `shifde.packaging/1`, its manifest exactly the SP3 one. A version with
screens is a generated app, compiled by `shifde.packaging/2`: its manifest adds `screens` and
`app.generator_version`, and its report has four stricter mandatory checks: the `entrypoint` is an
agent, each `screens.<i>` is a chat screen or a table screen of a pinned tool (by artifact key)
whose inputs are that tool's parameters, `models` counts exactly one model pin (a route counts),
and each `tools.<pin>` is a `connection.sql` tool on a connection of the solution's platform the
sealer can read: Databricks for `databricks.app/v1`, Snowflake for `snowflake.native-app/v1`
(`schemas.TARGET_ADAPTERS`), as each bound connection must be too. A Snowflake solution is only
ever a generated app (Call S6-H): its versions always get the `/2` rules, a version without screens
is refused (`release_incompatible`) with a failing `screens` diagnostic, and its manifest's
`app.generator_version` is `shifde.appgen.snowflake/1.0` (`APP_GENERATOR_VERSIONS`). A Docker
solution (`docker.compose/v1`) is the same under the Databricks `/2` rules, with generator
`shifde.appgen.docker/1.0`. A Fabric (`fabric.solution/v1`) or Palantir Marketplace
(`palantir.marketplace/v1`) solution is compiled by `/1` only: a version with screens gets a
failing `screens` diagnostic and every pinned connection a failing `connections.<pin>` (no data
connection of those platforms exists). `palantir.superrepo/v1` and `fabric.iq/v1` are refused at
solution create with a one-diagnostic `release_incompatible` report, and nothing is written.
The screens check sees only the pinned tools the sealer can see: a screen naming the key of a
hidden tool gets the answer a screen naming a tool that is not pinned gets, so neither the key nor
the parameters of a hidden tool can be probed (issue #316).

A model or connection is hidden from the caller when only versions the caller cannot see name it
(a version's own data reads always count as seen), and a connection also when the caller cannot
read it. Its paths in the refused report, its audit row and a release's `compatibility` read
`models.hidden` / `connections.hidden` (as a hidden tool's read `tools.hidden`); a release drops
its binding entries and nulls each data-read or bound connection id the reader cannot read (the
stored manifest and its digest are unchanged). A binding key that names a hidden model or
connection is refused exactly like a key that names nothing (issue #93, B-2741).

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

# the databricks.app/v1 compatibility check (design section 5, settled calls 14 and 15); a
# version with screens is a generated app, compiled by `/2` (SP5 design 6.2)
COMPILER_VERSION = "shifde.packaging/1"
COMPILER_VERSION_APP = "shifde.packaging/2"
APP_GENERATOR_VERSION = "shifde.appgen/1.0"  # equal to `appgen.render.GENERATOR_VERSION` (tested)
# a `/2` manifest's `app.generator_version`, by target kind (SP6 design section 4)
APP_GENERATOR_VERSIONS = {
    s.TARGET_KIND: APP_GENERATOR_VERSION,
    s.SNOWFLAKE_TARGET_KIND: "shifde.appgen.snowflake/1.0",
    s.DOCKER_TARGET_KIND: "shifde.appgen.docker/1.0",
}
SCREEN_KINDS = frozenset({"chat", "table"})
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
NOT_SNOWFLAKE = (
    "the bound connection is not a Snowflake connection of this organization in the solution's "
    "scope"
)
NOT_PLATFORM = {
    s.TARGET_KIND: NOT_DATABRICKS,
    s.SNOWFLAKE_TARGET_KIND: NOT_SNOWFLAKE,
    s.DOCKER_TARGET_KIND: NOT_DATABRICKS,
}
NO_EVALUATION = "a release needs an evaluation dataset"
DATASET_REFUSED = "the evaluation dataset is not a dataset of this project you can read"
DATASET_READ = "evaluation:dataset:read"
ONE_AGENT = "a generated app runs one agent"
ONE_MODEL = "a generated app uses exactly one model"
APP_TOOLS = "a generated app's tools are SQL tools over a Databricks connection"
APP_TOOLS_SNOWFLAKE = "a generated app's tools are SQL tools over a Snowflake connection"
APP_TOOLS_BY_KIND = {
    s.TARGET_KIND: APP_TOOLS,
    s.SNOWFLAKE_TARGET_KIND: APP_TOOLS_SNOWFLAKE,
    s.DOCKER_TARGET_KIND: APP_TOOLS,
}
NEEDS_SCREENS = "a Snowflake Native App needs screens"
# kinds that are only ever a generated app (`/2`): a version without screens is refused
APP_ONLY = {
    s.SNOWFLAKE_TARGET_KIND: NEEDS_SCREENS,
    s.DOCKER_TARGET_KIND: "a Docker app needs screens",
}
# kinds compiled by `/1` only: a version with screens is refused, and so is any pinned connection
NO_SCREENS = {
    s.FABRIC_TARGET_KIND: "a Fabric solution has no screens",
    s.PALANTIR_TARGET_KIND: "a Palantir Marketplace solution has no screens",
}
NO_CONNECTION = {
    s.FABRIC_TARGET_KIND: "a Fabric release binds no data connection",
    s.PALANTIR_TARGET_KIND: "a Palantir Marketplace release binds no data connection",
}
HIDDEN_TOOL = "tools.hidden"
HIDDEN_MODEL = "models.hidden"
HIDDEN_CONNECTION = "connections.hidden"
HIDDEN_PATHS = {"tools": HIDDEN_TOOL, "models": HIDDEN_MODEL, "connections": HIDDEN_CONNECTION}
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
    (`own`, `shared` or `route`) and connections, and the models and connections a source the
    caller can see names."""

    tools: dict[str, VersionAccess] = field(default_factory=dict)
    tool_specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    connections: set[str] = field(default_factory=set)
    seen_models: set[str] = field(default_factory=set)
    seen_connections: set[str] = field(default_factory=set)

    def model(self, name: str, kind: str, *, seen: bool) -> None:
        if self.models.get(name) != "shared":  # one shared sighting makes it shared
            self.models[name] = kind
        if seen:
            self.seen_models.add(name)

    def connection(self, name: str, *, seen: bool) -> None:
        self.connections.add(name)
        if seen:
            self.seen_connections.add(name)


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
        spec = json.loads(json.dumps(row["spec"]))
        for read in spec["data_reads"]:
            if not self._readable(read["connection_id"]):
                read["connection_id"] = None
        return s.SolutionVersionOut(
            id=row["id"],
            solution_id=row["solution_id"],
            version_no=row["version_no"],
            entrypoint=s.EntrypointOut(
                kind=row["entrypoint_kind"],
                version_id=row["entrypoint_version_id"] if shown else None,
                spec_hash=entry.spec_hash if entry is not None and shown else None,
            ),
            spec=spec,
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
            "screens": [screen.model_dump(mode="json") for screen in body.screens],
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
        if body.target_kind in s.UNSUPPORTED_TARGET_KINDS:
            diagnostic = _diagnostic("target_kind", s.UNSUPPORTED_TARGET_KINDS[body.target_kind])
            raise ReleaseIncompatible(
                "The target kind is not supported.",
                errors=[
                    {
                        "loc": ["compatibility", "target_kind"],
                        "msg": diagnostic["reason"],
                        "type": diagnostic["status"],
                        **diagnostic,
                    }
                ],
            )
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

    def active_named(self, ws: str, proj: str, name: str) -> s.SolutionOut | None:
        """The project's active solution named `name`, or None."""
        project = self._project(ws, proj)
        self.sc.pdp.require(READ, project)
        assert project.project_id is not None
        solution_id = repository.active_solution_id(
            self.sc.session, project.project_id, name, tenant_id=self.sc.tenant_id
        )
        return None if solution_id is None else self.get(ws, proj, solution_id)

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
        for read in version["spec"]["data_reads"]:
            found.connection(str(read["connection_id"]), seen=True)
        for version_id in dict.fromkeys(
            [version["entrypoint_version_id"], *(p.version_id for p in pins)]
        ):
            access = self.artifacts.version_access(version_id)
            if access is None:
                continue
            seen = access.visible
            if access.kind == "pipelines":
                for ref in self.artifacts.pipeline_resource_refs(version_id):
                    if ref.ref_type == "connection":
                        found.connection(str(ref.ref_id), seen=seen)
                    else:
                        found.model(str(ref.ref_id), "own" if ref.own else "shared", seen=seen)
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
                    found.connection(str(connection_id), seen=seen)
            elif access.kind == "agents":
                model = spec.get("model")
                if not isinstance(model, dict):
                    continue
                if "route_id" in model:
                    found.model(f"route-{model['route_id']}", "route", seen=seen)
                    continue
                for value in [model.get("model_id"), *(model.get("fallback_model_ids") or [])]:
                    model_id = _uuid(value)
                    if model_id is not None:
                        own = self.models.own_model(model_id)
                        found.model(str(model_id), "own" if own else "shared", seen=seen)
        return found

    def _readable(self, connection: str) -> bool:
        """Whether the caller may read the connection `connection` (an id string)."""
        connection_id = _uuid(connection)
        return connection_id is not None and self.conns.placement(connection_id) is not None

    def _hidden(self, found: _Pinned) -> Callable[[str, str], bool]:
        """Whether pin `name` of group `kind` (`tools`, `models`, `connections`) is hidden from
        the caller: a tool version the caller cannot see, a model no seen source names, and a
        connection no seen source names or the caller cannot read."""
        readable: dict[str, bool] = {}

        def hidden(kind: str, name: str) -> bool:
            if kind == "tools":
                access = found.tools.get(name)
                return access is None or not access.visible
            if kind == "models":
                return name not in found.seen_models
            if kind == "connections":
                if name not in found.seen_connections:
                    return True
                if name not in readable:
                    readable[name] = self._readable(name)
                return not readable[name]
            return False

        return hidden

    def _check_binding_keys(self, bindings: s.EnvironmentBindings, found: _Pinned) -> None:
        """A key that names no pin and a key that names a hidden model or connection get one
        answer, so a sealer cannot learn what a pin it cannot see names."""
        hidden = self._hidden(found)
        errors = [
            {
                "loc": ["body", "environment_bindings", "model_bindings", key],
                "msg": UNKNOWN_BINDING,
                "type": "binding",
            }
            for key in bindings.model_bindings
            if key not in found.models or hidden("models", key)
        ] + [
            {
                "loc": ["body", "environment_bindings", "connection_bindings", key],
                "msg": UNKNOWN_BINDING,
                "type": "binding",
            }
            for key in bindings.connection_bindings
            if key not in found.connections or hidden("connections", key)
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
        kind = solution["target_kind"]
        if kind in NO_CONNECTION:
            return NO_CONNECTION[kind]
        if bound is None:
            return NO_CONNECTION_BINDING
        placement = self.conns.placement(bound)
        if (
            placement is None
            or placement.adapter_key != s.TARGET_ADAPTERS[kind]
            or not in_scope(solution, placement.workspace_id, placement.project_id)
        ):
            return NOT_PLATFORM[kind]
        return None

    def _app_tool_reason(self, solution: Mapping[str, Any], spec: Mapping[str, Any]) -> str | None:
        """None for a `connection.sql` tool on a connection of the solution's platform (Databricks
        or Snowflake, by its target kind) the caller can read."""
        kind = solution["target_kind"]
        binding = spec.get("binding")
        connection_id = _uuid(binding.get("connection_id")) if isinstance(binding, dict) else None
        placement = self.conns.placement(connection_id) if connection_id is not None else None
        if (
            spec.get("executor") != "connection.sql"
            or placement is None
            or placement.adapter_key != s.TARGET_ADAPTERS[kind]
        ):
            return APP_TOOLS_BY_KIND[kind]
        return None

    @staticmethod
    def _screen_reason(screen: Mapping[str, Any], tools: Mapping[str, Any]) -> str | None:
        """None for a chat screen, or a table screen of a pinned tool (`tools`: artifact key ->
        spec) whose inputs are that tool's parameters; else the problem."""
        kind = screen.get("kind")
        if kind not in SCREEN_KINDS:
            return f"the screen kind {kind!r} is not chat or table"
        if kind == "chat":
            return None
        tool = screen.get("tool")
        spec = tools.get(tool) if isinstance(tool, str) else None
        if spec is None:
            return f"the version pins no tool {tool!r}"
        schema = spec.get("input_schema")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        for name in screen.get("inputs") or []:
            if not isinstance(properties, dict) or name not in properties:
                return f"{name!r} is not a parameter of tool {tool!r}"
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
        name), evaluation; a version with screens (`/2`) also gets `models` and one `screens.<i>`
        per screen, and stricter entrypoint and tool rules. A Snowflake solution's version always
        gets the `/2` rules, and without screens a failing `screens` (S6-H); so does a Docker
        solution's. A Fabric or Palantir solution's version gets only the `/1` rules, and with
        screens a failing `screens`. Every one is mandatory (no accepted deviations, B-2699)."""
        kind = solution["target_kind"]
        screens = version["spec"].get("screens") or []
        app = (bool(screens) and kind not in NO_SCREENS) or kind in APP_ONLY
        published = (
            entry.state == "published"
            and entry.kind == ENTRYPOINT_KINDS[version["entrypoint_kind"]]
        )
        entrypoint = None if published else ENTRYPOINT_GONE
        if app and entrypoint is None and version["entrypoint_kind"] != "agent":
            entrypoint = ONE_AGENT
        report = [_diagnostic("entrypoint", entrypoint)]
        report += [
            _diagnostic(
                f"tools.{name}",
                _tool_reason(found.tool_specs[name])
                or (self._app_tool_reason(solution, found.tool_specs[name]) if app else None),
            )
            for name in sorted(found.tools)
        ]
        if app:
            report.append(_diagnostic("models", None if len(found.models) == 1 else ONE_MODEL))
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
        if kind in NO_SCREENS:
            if screens:
                report.append(_diagnostic("screens", NO_SCREENS[kind]))
        elif screens:
            tools = {
                self.artifacts.artifact_row("tools", access.artifact_id)["key"]: (
                    found.tool_specs[name]
                )
                for name, access in found.tools.items()
                if access.visible  # a hidden tool reads exactly like a tool that is not pinned
            }
            report += [
                _diagnostic(f"screens.{i}", self._screen_reason(screen, tools))
                for i, screen in enumerate(screens)
            ]
        elif kind in APP_ONLY:
            report.append(_diagnostic("screens", APP_ONLY[kind]))
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
    def _shown_path(path: str, hidden: Callable[[str, str], bool]) -> str:
        """`tools.<pin>` of a tool version the caller cannot see is `tools.hidden`; `models.<pin>`
        and `connections.<pin>` of a hidden model or connection (`_hidden`) are `models.hidden`
        and `connections.hidden`."""
        kind, _, name = path.partition(".")
        if kind in HIDDEN_PATHS and name and hidden(kind, name):
            return HIDDEN_PATHS[kind]
        return path

    def _manifest(
        self,
        solution: Mapping[str, Any],
        version: Mapping[str, Any],
        entry: VersionAccess,
        pins: Sequence[PinnedVersion],
        bindings: s.EnvironmentBindings,
    ) -> dict[str, Any]:
        """The release manifest: no digest, sealing time or sealer (settled call 12). A version
        with screens adds `screens` and `app` and is compiled by `/2`; any other gets exactly the
        `/1` manifest."""
        evaluation = version["spec"]["evaluation"]
        dataset = self._dataset(solution, _uuid(evaluation["dataset_id"]))
        assert dataset is not None  # the report's `evaluation` diagnostic was supported
        manifest: dict[str, Any] = {
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
        screens = version["spec"].get("screens") or []
        if screens:
            manifest["screens"] = screens
            manifest["app"] = {"generator_version": APP_GENERATOR_VERSIONS[solution["target_kind"]]}
            manifest["compiler_version"] = COMPILER_VERSION_APP
        return manifest

    def _release_out(self, row: Mapping[str, Any]) -> s.ReleaseOut:
        """The release, redacted for the caller (the stored row is not): the manifest's
        entrypoint and pins of versions the caller cannot see lose their version id and hash;
        hidden tool, model and connection paths read `tools.hidden`, `models.hidden` and
        `connections.hidden` (`_shown_path`); binding entries of hidden models and connections
        are dropped; and a data-read or bound connection id the caller cannot read is null."""
        version = repository.get_version(
            self.sc.session, row["solution_version_id"], tenant_id=self.sc.tenant_id
        )
        assert version is not None  # a release's version is never deleted (restrict key)
        pins = [
            PinnedVersion(
                role=p["role"], version_id=UUID(p["version_id"]), spec_hash=p["spec_hash"]
            )
            for p in row["manifest"]["pins"]
        ]
        hidden = self._hidden(self._pinned(version, pins))
        manifest = json.loads(json.dumps(row["manifest"]))
        for item in [manifest["entrypoint"], *manifest["pins"]]:
            version_id = _uuid(item["version_id"])
            if version_id is None or not self._seen(version_id):
                item["version_id"] = item["spec_hash"] = None
        for read in manifest["data_reads"]:
            if not self._readable(read["connection_id"]):
                read["connection_id"] = None
        bound = manifest["bindings"]
        bound["model_bindings"] = {
            k: v for k, v in bound["model_bindings"].items() if not hidden("models", k)
        }
        bound["connection_bindings"] = {
            k: v if self._readable(v) else None
            for k, v in bound["connection_bindings"].items()
            if not hidden("connections", k)
        }
        compatibility = [
            s.Diagnostic(**{**d, "path": self._shown_path(d["path"], hidden)})
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
            hidden = self._hidden(found)

            def shown(path: str) -> str:
                return self._shown_path(path, hidden)

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
