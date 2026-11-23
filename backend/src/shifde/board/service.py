"""The board (sub-project 7 design 7.1 and 7.2; B-3922 points 4, 5, 46-53 and 70).

Visibility comes only from `pdp.visible_scopes`, never from `require`, so no denial is recorded
for an object the board leaves out:

- projects: `project:read`; archived projects are left out of the organization board, a
  readable archived project is shown on its own board;
- solutions and releases: `packaging:solution:read` covering the project, else no solutions;
- evaluation: `evaluation:run:read` covering the project, else null;
- targets: `delivery:target:read` at the target's own scope (its workspace for a workspace-level
  target, its project otherwise); hidden targets are not mentioned;
- cells: `delivery:deployment:read` (current, last attempt) and `delivery:plan:read` (open plan)
  covering the target's scope, else null.

The `workspace`/`project` filters resolve through `tenancy.service.scope_ref_from_slugs`: an
unknown slug and an invisible one both give an empty board on the organization route and 404 on
the single-project route. The board writes nothing: no audit row, and no plan or deployment
service (their reads run the settle steps).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any  # Any: rows
from uuid import UUID

from shifde.authz.service import ScopeFilter
from shifde.board import repository
from shifde.board import schemas as s
from shifde.core.clock import utcnow
from shifde.core.errors import NotFound
from shifde.core.pagination import decode_cursor, encode_cursor, paginate
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

DEFAULT_PROJECTS = 20
MAX_PROJECTS = 50
MAX_SOLUTIONS = 200
MAX_RELEASES = 5

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values
Cell = tuple[UUID, UUID]  # (solution id, target id)


def _filter_args(f: ScopeFilter) -> dict[str, Any]:  # Any: repository keyword values
    return {
        "tenant_wide": f.tenant_wide,
        "workspace_ids": sorted(f.workspace_ids),
        "project_ids": sorted(f.project_ids),
    }


def _covers_target(f: ScopeFilter, target: Row) -> bool:
    if target["project_id"] is None:
        return f.covers_workspace(target["workspace_id"])
    return f.covers_project(target["workspace_id"], target["project_id"])


def _by_cell(rows: Sequence[Row], key: str = "target_id") -> dict[Cell, Row]:
    return {(r["solution_id"], r[key]): r for r in rows}


class BoardService:
    def __init__(self, scope: TenantScope) -> None:
        self.sc = scope

    def org_board(
        self, *, workspace: str | None, project: str | None, limit: int, cursor: str | None
    ) -> s.BoardOut:
        after = decode_cursor(cursor)
        ws_id = pr_id = None
        if workspace or project:
            try:
                ref = tenancy.scope_ref_from_slugs(self.sc, workspace, project)
            except NotFound:
                return self._empty()
            ws_id, pr_id = ref.workspace_id, ref.project_id
        f = self.sc.pdp.visible_scopes("project:read")
        if not (f.tenant_wide or f.workspace_ids or f.project_ids):
            return self._empty()
        rows = repository.list_projects(
            self.sc.session,
            tenant_id=self.sc.tenant_id,
            **_filter_args(f),
            workspace_id=ws_id,
            project_id=pr_id,
            include_archived=False,
            after=after,
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return s.BoardOut(
            generated_at=utcnow(),
            projects=self._projects(page),
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def project_board(self, ws: str, proj: str) -> s.BoardOut:
        ref = tenancy.scope_ref_from_slugs(self.sc, ws, proj)  # 404 unknown or unreadable
        rows = repository.list_projects(
            self.sc.session,
            tenant_id=self.sc.tenant_id,
            **_filter_args(self.sc.pdp.visible_scopes("project:read")),
            workspace_id=ref.workspace_id,
            project_id=ref.project_id,
            include_archived=True,
            after=None,
            limit=1,
        )
        if not rows:
            raise NotFound("project not found")
        return s.BoardOut(generated_at=utcnow(), projects=self._projects(rows), next_cursor=None)

    @staticmethod
    def _empty() -> s.BoardOut:
        return s.BoardOut(generated_at=utcnow(), projects=[], next_cursor=None)

    # ------------------------------------------------------------------ assembly
    def _projects(self, projects: Sequence[Row]) -> list[s.BoardProject]:
        if not projects:
            return []
        pdp, session, tenant = self.sc.pdp, self.sc.session, self.sc.tenant_id
        sol_f = pdp.visible_scopes("packaging:solution:read")
        eval_f = pdp.visible_scopes("evaluation:run:read")
        target_f = pdp.visible_scopes("delivery:target:read")
        dep_f = pdp.visible_scopes("delivery:deployment:read")
        plan_f = pdp.visible_scopes("delivery:plan:read")
        project_ws = {p["id"]: p["workspace_id"] for p in projects}

        # solutions (at most MAX_SOLUTIONS shown per project; one more tells `truncated`)
        sol_projects = [pid for pid, wid in project_ws.items() if sol_f.covers_project(wid, pid)]
        solutions: dict[UUID, list[Row]] = defaultdict(list)
        if sol_projects:
            for r in repository.solutions(
                session, tenant_id=tenant, project_ids=sol_projects, per_project=MAX_SOLUTIONS + 1
            ):
                solutions[r["project_id"]].append(r)
        shown = [r for pid in sol_projects for r in solutions[pid][:MAX_SOLUTIONS]]
        solution_project = {r["id"]: r["project_id"] for r in shown}

        # releases and their latest evaluation run
        releases: dict[UUID, list[Row]] = defaultdict(list)
        if shown:
            for r in repository.releases(
                session,
                tenant_id=tenant,
                solution_ids=list(solution_project),
                per_solution=MAX_RELEASES,
            ):
                releases[r["solution_id"]].append(r)
        eval_releases = [
            r["id"]
            for sid, rs in releases.items()
            for r in rs
            if eval_f.covers_project(project_ws[solution_project[sid]], solution_project[sid])
        ]
        evaluations = (
            {
                r["release_id"]: r
                for r in repository.latest_evaluations(
                    session, tenant_id=tenant, release_ids=eval_releases
                )
            }
            if eval_releases
            else {}
        )

        # visible targets of each project (its own and its workspace's)
        targets: list[Row] = []
        if shown:
            targets = [
                t
                for t in repository.targets(
                    session,
                    tenant_id=tenant,
                    project_ids=list(project_ws),
                    workspace_ids=sorted(set(project_ws.values())),
                )
                if _covers_target(target_f, t)
            ]
        dep_targets = [t["id"] for t in targets if _covers_target(dep_f, t)]
        plan_targets = [t["id"] for t in targets if _covers_target(plan_f, t)]
        sids = list(solution_project)
        current: dict[Cell, Row] = {}
        last: dict[Cell, Row] = {}
        plans: dict[Cell, Row] = {}
        if sids and dep_targets:
            current = _by_cell(
                repository.current_deployments(
                    session, tenant_id=tenant, solution_ids=sids, target_ids=dep_targets
                )
            )
            last = _by_cell(
                repository.last_attempts(
                    session, tenant_id=tenant, solution_ids=sids, target_ids=dep_targets
                )
            )
        if sids and plan_targets:
            plans = _by_cell(
                repository.open_plans(
                    session, tenant_id=tenant, solution_ids=sids, target_ids=plan_targets
                )
            )

        def project_targets(pid: UUID) -> list[Row]:
            return [
                t
                for t in targets
                if t["project_id"] == pid
                or (t["project_id"] is None and t["workspace_id"] == project_ws[pid])
            ]

        def release_out(r: Row) -> s.BoardRelease:
            run = evaluations.get(r["id"])
            return s.BoardRelease(
                id=r["id"],
                version_no=r["version_no"],
                digest12=r["digest"][:12],
                status=r["status"],
                sealed_at=r["sealed_at"],
                evaluation=s.BoardEvaluation(run_id=run["id"], status=run["status"])
                if run
                else None,
            )

        def target_out(sid: UUID, t: Row) -> s.BoardTarget:
            cell = (sid, t["id"])
            cur, att, plan = current.get(cell), last.get(cell), plans.get(cell)
            return s.BoardTarget(
                target_id=t["id"],
                name=t["name"],
                environment=t["environment"],
                platform_kind=t["platform_kind"],
                current=s.BoardCurrent(
                    release_id=cur["release_id"],
                    version_no=cur["version_no"],
                    deployment_id=cur["deployment_id"],
                    verified_at=cur["verified_at"],
                )
                if cur
                else None,
                last_attempt=s.BoardAttempt(
                    deployment_id=att["deployment_id"],
                    release_id=att["release_id"],
                    status=att["status"],
                    status_reason=att["status_reason"],
                    updated_at=att["updated_at"],
                )
                if att
                else None,
                open_plan=s.BoardOpenPlan(
                    plan_id=plan["plan_id"],
                    release_id=plan["release_id"],
                    status=plan["status"],
                    expires_at=plan["expires_at"],
                )
                if plan
                else None,
            )

        out: list[s.BoardProject] = []
        for p in projects:
            pid = p["id"]
            mine = project_targets(pid)
            out.append(
                s.BoardProject(
                    workspace=s.BoardRef(
                        id=p["workspace_id"], slug=p["workspace_slug"], name=p["workspace_name"]
                    ),
                    project=s.BoardRef(id=pid, slug=p["slug"], name=p["name"]),
                    truncated=len(solutions[pid]) > MAX_SOLUTIONS,
                    solutions=[
                        s.BoardSolution(
                            id=r["id"],
                            name=r["name"],
                            status=r["status"],
                            latest_version_no=r["latest_version_no"],
                            releases=[release_out(x) for x in releases[r["id"]]],
                            targets=[target_out(r["id"], t) for t in mine],
                        )
                        for r in solutions[pid][:MAX_SOLUTIONS]
                    ],
                )
            )
        return out
