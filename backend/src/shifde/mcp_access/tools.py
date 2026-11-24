"""The v1 MCP tools (design 3.3 and 10 items 8 to 14): one argument model each (no extra keys)
and a sync handler that calls the owning module's service with the caller's `TenantScope`.

Handlers run on a worker thread inside a database session of their own (`server.Gate`). Every id
of an argument resolves only in the key's client and resource scope: the services answer
`NotFound` for anything else, and the gate turns every `NotFound` into one `not_found` text. No
tool approves, rejects, cancels, applies, seals or plans anything (Call S8-D). `board_get`,
`assist_ask` and `assist_get` (SP8-3) answer the routes' documents; a proposal is accepted or
dismissed in SETH's screens only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any  # Any: JSON tool results
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from shifde.adapters.runtime import IntegrationRuntime
from shifde.assist.schemas import AssistRequestIn
from shifde.assist.service import AssistService
from shifde.authz.service import ResourceRef
from shifde.board.service import BoardService
from shifde.build.service import ArtifactService
from shifde.core.bodies import Body
from shifde.core.errors import NotFound
from shifde.core.pagination import MAX_LIMIT, decode_cursor, encode_cursor, paginate
from shifde.delivery.deployments import DeploymentService
from shifde.mcp_access.schemas import DEFAULT_TOOLS
from shifde.mcp_access.store import SettingsRow
from shifde.packaging import schemas as packaging_schemas
from shifde.packaging.service import SolutionService
from shifde.runtime.governance import GovernanceHooks
from shifde.runtime.schemas import RunCreate
from shifde.runtime.service import ApprovalService, RunService
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

Result = dict[str, Any]  # Any: JSON tool results


# ------------------------------------------------------------------ arguments
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class ListArgs(_Args):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=100)


class ProjectListArgs(ListArgs):
    project_id: UUID


class StartRunArgs(_Args):
    project_id: UUID
    agent_version_id: UUID
    input: str = Field(min_length=1, max_length=32768)
    max_cost: float | None = Field(default=None, gt=0)


class RunArgs(_Args):
    run_id: UUID


class ReleaseArgs(_Args):
    release_id: UUID


class DeploymentArgs(_Args):
    deployment_id: UUID


class BoardArgs(_Args):
    project_id: UUID


class AssistAskArgs(AssistRequestIn, Body, _Args):  # `Body`: no text PostgreSQL cannot store
    project_id: UUID


class AssistGetArgs(_Args):
    request_id: UUID


# ------------------------------------------------------------------ the caller
@dataclass(frozen=True)
class Env:
    """What a handler needs beyond the scope: the app's runtime and governance hooks, the
    client's MCP settings and the key's name and expiry."""

    runtime: IntegrationRuntime
    hooks: GovernanceHooks
    mcp: SettingsRow
    key_name: str
    key_expires_at: datetime


Handler = Callable[[TenantScope, Env, Any], Result]  # Any: the tool's argument model


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[_Args]
    permission: str
    handler: Handler


def allowed_tools(mcp: SettingsRow) -> tuple[str, ...]:
    """The tools the client's keys may call: `allowed_tools`, or the default set when NULL."""
    return mcp.allowed_tools if mcp.allowed_tools is not None else DEFAULT_TOOLS


def _held(scope: TenantScope, permission: str) -> bool:
    if permission == "mcp:access":  # checked at organization level before any tool runs
        return True
    f = scope.pdp.visible_scopes(permission)
    return f.tenant_wide or bool(f.workspace_ids or f.project_ids)


def _need(scope: TenantScope, permission: str) -> None:
    """`Forbidden` (with its decision record) unless the caller holds `permission` somewhere."""
    if not _held(scope, permission):
        scope.pdp.require(permission, scope.tenant_ref)


def _project(scope: TenantScope, project_id: UUID) -> tuple[ResourceRef, str, str]:
    """(ref, workspace slug, project slug) of a project of this client (else `NotFound`); the
    services called with the slugs decide its visibility."""
    ref = tenancy.resolve_scope_ref(scope, "project", project_id)
    assert ref.workspace_id is not None and ref.project_id is not None
    workspaces, projects = tenancy.scope_slug_map(scope, [ref.workspace_id], [ref.project_id])
    return ref, workspaces[ref.workspace_id], projects[ref.project_id]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _page(items: list[Result], args: ListArgs) -> Result:
    """Keyset page over items sorted by their `id` (UUIDv7, time-ordered)."""
    after = decode_cursor(args.cursor)
    rows = sorted(
        (i for i in items if after is None or UUID(i["id"]) > after), key=lambda i: i["id"]
    )
    page, more = paginate(rows, args.limit)
    return {
        "items": page,
        "next_cursor": encode_cursor(UUID(page[-1]["id"])) if more and page else None,
    }


# ------------------------------------------------------------------ handlers
def whoami(scope: TenantScope, env: Env, _: NoArgs) -> Result:
    return {
        "key_name": env.key_name,
        "api_key_id": str(scope.principal.api_key_id),
        "principal_type": scope.principal.principal_type,
        "client": scope.tenant_name,
        "client_slug": scope.tenant_slug,
        "expires_at": _iso(env.key_expires_at),
        "tools": [t for t in allowed_tools(env.mcp) if _held(scope, TOOLS[t].permission)],
    }


def list_projects(scope: TenantScope, _: Env, args: ListArgs) -> Result:
    _need(scope, "project:read")
    page = tenancy.list_readable_projects(scope, limit=args.limit, cursor=args.cursor)
    items = [
        {
            "id": str(p.id),
            "slug": p.slug,
            "name": p.name,
            "workspace": p.workspace_slug,
            "status": p.status,
        }
        for p in page.items
    ]
    return {"items": items, "next_cursor": page.next_cursor}


def list_agents(scope: TenantScope, env: Env, args: ProjectListArgs) -> Result:
    ref, _, _ = _project(scope, args.project_id)
    scope.pdp.require("agent:read", ref, hide_unless="project:read")
    arts = ArtifactService(scope, env.runtime)
    page = arts.list_artifacts(
        "agents",
        scope_type="project",
        scope_id=ref.project_id,
        status=None,
        q=None,
        limit=args.limit,
        cursor=args.cursor,
    )
    items = [
        {
            "id": str(a.id),
            "key": a.key,
            "name": a.name,
            "status": a.status,
            "versions": [
                {
                    "version_id": str(v.id),
                    "version_no": v.version_no,
                    "published_at": _iso(v.published_at),
                }
                for v in arts.list_versions("agents", a.id)
                if v.state == "published"
            ],
        }
        for a in page.items
    ]
    return {"items": items, "next_cursor": page.next_cursor}


def start_run(scope: TenantScope, env: Env, args: StartRunArgs) -> Result:
    """A run of a published agent version in a project, as the key's principal. `max_cost` is
    `min(argument, max_cost_per_call)`; the ledger's budget pre-flight and reservation apply
    unchanged, and a run that reaches an approval gate waits for a person in SETH's screens."""
    _, ws_slug, project_slug = _project(scope, args.project_id)
    access = ArtifactService(scope, env.runtime).version_access(args.agent_version_id)
    if access is None or access.kind != "agents" or not access.visible:
        raise NotFound("agent version not found")
    cap = env.mcp.max_cost_per_call
    max_cost = cap if args.max_cost is None else min(Decimal(str(args.max_cost)), cap)
    body = RunCreate(
        kind="agent",
        agent_id=access.artifact_id,
        version_id=access.version_id,
        workspace=ws_slug,
        project=project_slug,
        input=args.input,
    )
    run, _ = RunService(scope, env.runtime, env.hooks).create(body, None, max_cost=max_cost)
    return {"run_id": str(run.id), "status": run.status}


def get_run(scope: TenantScope, env: Env, args: RunArgs) -> Result:
    """Status, usage and the final output as the run routes give them (retention-scrubbed or
    withheld by the client's capture setting); pending approvals as a count only."""
    runs = RunService(scope, env.runtime, env.hooks)
    run = runs.get(args.run_id)
    result = runs.result(args.run_id)
    approvals = ApprovalService(scope, env.runtime)
    pending, cursor = 0, None
    while True:
        page = approvals.list_approvals("pending", None, MAX_LIMIT, cursor, tree_run_id=run.id)
        pending += len(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    return {
        "run_id": str(run.id),
        "kind": run.kind,
        "status": run.status,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "error_class": run.error_class,
        "usage": run.usage.model_dump(),
        "output": result.output,
        "redacted": result.redacted,
        "pending_approvals": pending,
    }


def _release(r: packaging_schemas.ReleaseOut) -> Result:
    """A release without its manifest (no binding values)."""
    return {
        "id": str(r.id),
        "solution_id": str(r.solution_id),
        "solution_version_id": str(r.solution_version_id),
        "version_no": r.version_no,
        "workspace": r.workspace,
        "project": r.project,
        "digest": r.digest,
        "status": r.status,
        "sealed_at": _iso(r.sealed_at),
    }


def list_releases(scope: TenantScope, env: Env, args: ProjectListArgs) -> Result:
    _, ws, project = _project(scope, args.project_id)
    solutions = SolutionService(scope, env.runtime)
    items: list[Result] = []
    sol_cursor: str | None = None
    while True:
        sols = solutions.list(ws, project, status=None, limit=MAX_LIMIT, cursor=sol_cursor)
        for sol in sols.items:
            ver_cursor: str | None = None
            while True:
                versions = solutions.list_versions(
                    ws, project, sol.id, limit=MAX_LIMIT, cursor=ver_cursor
                )
                for version in versions.items:
                    rel_cursor: str | None = None
                    while True:
                        releases = solutions.list_releases(
                            ws, project, sol.id, version.id, limit=MAX_LIMIT, cursor=rel_cursor
                        )
                        items += [_release(r) for r in releases.items]
                        rel_cursor = releases.next_cursor
                        if rel_cursor is None:
                            break
                ver_cursor = versions.next_cursor
                if ver_cursor is None:
                    break
        sol_cursor = sols.next_cursor
        if sol_cursor is None:
            break
    return _page(items, args)


def get_release(scope: TenantScope, env: Env, args: ReleaseArgs) -> Result:
    return _release(SolutionService(scope, env.runtime).get_release(args.release_id))


def _deployment(d: Any) -> Result:  # Any: DeploymentOut / DeploymentDetail
    return {
        "id": str(d.id),
        "status": d.status,
        "status_reason": d.status_reason,
        "created_at": _iso(d.created_at),
        "updated_at": _iso(d.updated_at),
    }


def list_deployments(scope: TenantScope, _: Env, args: ListArgs) -> Result:
    page = DeploymentService(scope).list(
        status=None, target=None, limit=args.limit, cursor=args.cursor
    )
    return {"items": [_deployment(d) for d in page.items], "next_cursor": page.next_cursor}


def get_deployment(scope: TenantScope, _: Env, args: DeploymentArgs) -> Result:
    """State, the verification checks (name and status) and the journal's step names; no
    artifact, plan body or address."""
    detail = DeploymentService(scope).get(args.deployment_id)
    checks = (detail.verification or {}).get("checks")
    return {
        **_deployment(detail),
        "checks": [
            {"name": c.get("name"), "status": c.get("status")}
            for c in (checks if isinstance(checks, list) else [])
            if isinstance(c, dict)
        ],
        "journal_steps": [step.step_key for step in detail.journal],
    }


def board_get(scope: TenantScope, env: Env, args: BoardArgs) -> Result:
    """The project's board as its route gives it: what the caller may not read is null or left
    out by the board itself."""
    _need(scope, "project:read")
    _, ws, project = _project(scope, args.project_id)
    return BoardService(scope).project_board(ws, project).model_dump(mode="json")


def assist_ask(scope: TenantScope, env: Env, args: AssistAskArgs) -> Result:
    """An assist request in a project, as the key's principal. The router call and the
    specialist's run together stay within `max_cost_per_call`; a refused or failed request is a
    result with its `status` and `status_reason`."""
    _need(scope, "assist:request:write")
    _, ws, project = _project(scope, args.project_id)
    body = AssistRequestIn(text=args.text, specialist=args.specialist, context=args.context)
    request = AssistService(scope, env.runtime, env.hooks).create_request(
        ws, project, body, max_cost=env.mcp.max_cost_per_call
    )
    return request.model_dump(mode="json")


def assist_get(scope: TenantScope, env: Env, args: AssistGetArgs) -> Result:
    """An assist request as its route gives it (proposal payloads and `result_ref` only with the
    read permissions of what they name)."""
    _need(scope, "assist:request:read")
    request = AssistService(scope, env.runtime, env.hooks).get_request(args.request_id)
    return request.model_dump(mode="json")


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "whoami",
            "The API key's name and expiry, its organization and the MCP tools it may call.",
            NoArgs,
            "mcp:access",
            whoami,
        ),
        Tool(
            "list_projects",
            "Projects the key can read.",
            ListArgs,
            "project:read",
            list_projects,
        ),
        Tool(
            "list_agents",
            "Agents of a project with their published versions.",
            ProjectListArgs,
            "agent:read",
            list_agents,
        ),
        Tool(
            "start_run",
            "Start a run of a published agent version in a project; returns the run id.",
            StartRunArgs,
            "agent:execute",
            start_run,
        ),
        Tool(
            "get_run",
            "A run's status, usage, final output and number of pending approvals.",
            RunArgs,
            "run:read",
            get_run,
        ),
        Tool(
            "list_releases",
            "Sealed releases of a project (without their manifests).",
            ProjectListArgs,
            "packaging:solution:read",
            list_releases,
        ),
        Tool(
            "get_release",
            "One release (without its manifest).",
            ReleaseArgs,
            "packaging:solution:read",
            get_release,
        ),
        Tool(
            "list_deployments",
            "Deployments the key can read: state only.",
            ListArgs,
            "delivery:deployment:read",
            list_deployments,
        ),
        Tool(
            "get_deployment",
            "One deployment: state, verification checks and journal step names.",
            DeploymentArgs,
            "delivery:deployment:read",
            get_deployment,
        ),
        Tool(
            "board_get",
            "A project's board: solutions, releases, evaluations and deployments per target.",
            BoardArgs,
            "project:read",
            board_get,
        ),
        Tool(
            "assist_ask",
            "Ask a project's specialists; returns the assist request and the run it started.",
            AssistAskArgs,
            "assist:request:write",
            assist_ask,
        ),
        Tool(
            "assist_get",
            "An assist request: routing decision, run status and proposals.",
            AssistGetArgs,
            "assist:request:read",
            assist_get,
        ),
    )
}
