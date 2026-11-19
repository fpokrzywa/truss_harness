"""Deployment routes: list, read, cancel, reconcile and mark-failed (design sections 4 and 9 items
1, 2, 7, 9, 10 and 11; DECISIONS B-2681..B-2687).

A deployment is scoped by its target's workspace/project; an unknown id, another organization's
id and a deployment the caller cannot read all get the same 404 `deployment not found`. Every
route checks permission first (the list: some visible scope or 403; an item route: the row and
`_require`), and only then runs the lost-job sweep and the plan and deployment settle steps and
commits them, so a refused caller never triggers housekeeping. It then reads the row again
(`FOR UPDATE` on the POST routes) and acts.

Cancel is allowed only while the deployment and its apply job are both `queued` (settled call 7).
Reconcile queues one read-only `reconcile` job for an `unknown_outcome` deployment and never
changes its status (settled call 11). Mark-failed moves an `unknown_outcome` deployment to
`resolved_failed` with the person, their note and the time. The three POST routes need
`delivery:deployment:resolve` and are audited.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any  # Any: deployment rows
from uuid import UUID

from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.core.errors import AppError, NotFound
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.delivery import gateway, repository, service
from shifde.delivery import schemas as s
from shifde.tenancy.scope import TenantScope


class DeploymentNotCancellable(AppError):
    status, code, title = 409, "deployment_not_cancellable", "Conflict"


class DeploymentNotUnknown(AppError):
    status, code, title = 409, "deployment_not_unknown", "Conflict"


class ReconcilePending(AppError):
    status, code, title = 409, "reconcile_pending", "Conflict"


class DeploymentService:
    def __init__(self, scope: TenantScope) -> None:
        self.sc = scope

    # ================================================================== helpers
    def _settle(self) -> None:
        """The lost-job sweep and the plan and deployment settle steps, committed."""
        session = self.sc.session
        repository.sweep_lost_jobs(session)
        service.settle_plans(session, self.sc.tenant_id)
        service.settle_deployments(session, self.sc.tenant_id)
        session.commit()

    def _ref(self, row: Mapping[str, Any]) -> ResourceRef:
        return ResourceRef(
            self.sc.tenant_id,
            row["workspace_id"],
            row["project_id"],
            resource_type="delivery_deployment",
            resource_id=row["id"],
        )

    def _get_row(self, deployment_id: UUID, *, for_update: bool = False) -> Mapping[str, Any]:
        row = repository.get_deployment(
            self.sc.session, deployment_id, tenant_id=self.sc.tenant_id, for_update=for_update
        )
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("deployment not found")
        return row

    def _require(self, action: str, row: Mapping[str, Any]) -> None:
        try:
            self.sc.pdp.require(action, self._ref(row), hide_unless="delivery:deployment:read")
        except NotFound as exc:
            # a deployment the caller cannot read gets the answer an unknown id gets
            raise NotFound("deployment not found") from exc

    def _permitted(self, deployment_id: UUID, action: str) -> None:
        """Permission on the deployment first, then the housekeeping (lead note 5, #69)."""
        self._require(action, self._get_row(deployment_id))
        self._settle()

    def _audit(self, action: str, row: Mapping[str, Any], **details: Any) -> None:
        audit.record(
            self.sc.session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type="delivery_deployment",
            target_id=row["id"],
            target_label=f"delivery_deployment:{row['application_key']}",
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details={
                "deployment_id": str(row["id"]),
                "plan_id": str(row["plan_id"]),
                "target_id": str(row["target_id"]),
                "application_key": row["application_key"],
                **details,
            },
        )

    @staticmethod
    def _out(row: Mapping[str, Any]) -> s.DeploymentOut:
        return s.DeploymentOut(
            id=row["id"],
            plan_id=row["plan_id"],
            target_id=row["target_id"],
            application_key=row["application_key"],
            status=row["status"],
            status_reason=row["status_reason"],
            receipt=row["receipt"],
            verification=row["verification"],
            reconcile_evidence=row["reconcile_evidence"],
            observe_deadline=row["observe_deadline"],
            resolved_by=row["resolved_by"],
            resolved_note=row["resolved_note"],
            resolved_at=row["resolved_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ================================================================== routes
    def list(
        self, *, status: str | None, target: UUID | None, limit: int, cursor: str | None
    ) -> Page[s.DeploymentOut]:
        pdp = self.sc.pdp
        flt = pdp.visible_scopes("delivery:deployment:read")
        if not (flt.tenant_wide or flt.workspace_ids or flt.project_ids):
            pdp.require("delivery:deployment:read", self.sc.tenant_ref)  # 403 + decision record
        self._settle()
        rows = repository.list_deployments(
            self.sc.session,
            tenant_id=self.sc.tenant_id,
            status=status,
            target_id=target,
            tenant_wide=flt.tenant_wide,
            visible_workspaces=sorted(flt.workspace_ids),
            visible_projects=sorted(flt.project_ids),
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, deployment_id: UUID) -> s.DeploymentDetail:
        self._permitted(deployment_id, "delivery:deployment:read")
        row = self._get_row(deployment_id)
        journal = repository.deployment_journal(
            self.sc.session, deployment_id, tenant_id=self.sc.tenant_id
        )
        return s.DeploymentDetail(
            **self._out(row).model_dump(),
            journal=[s.DeploymentJournalStep.model_validate(dict(j)) for j in journal],
        )

    def cancel(self, deployment_id: UUID) -> s.DeploymentOut:
        self._permitted(deployment_id, "delivery:deployment:resolve")
        session = self.sc.session
        row = self._get_row(deployment_id, for_update=True)
        jobs = repository.deployment_jobs(
            session, deployment_id, ("apply",), tenant_id=self.sc.tenant_id, for_update=True
        )
        if row["status"] != "queued" or not jobs or any(j["status"] != "queued" for j in jobs):
            raise DeploymentNotCancellable("only a queued deployment can be cancelled")
        for job in jobs:
            repository.cancel_job(session, job["id"], tenant_id=self.sc.tenant_id)
        repository.set_deployment_status(
            session,
            deployment_id,
            "cancelled",
            "cancelled before the apply was leased",
            expected=("queued",),
            tenant_id=self.sc.tenant_id,
        )
        cancelled = self._get_row(deployment_id)
        self._audit(
            "delivery.deployment.cancelled", cancelled, job_ids=[str(j["id"]) for j in jobs]
        )
        session.commit()
        return self._out(cancelled)

    def reconcile(self, deployment_id: UUID) -> s.DeploymentOut:
        self._permitted(deployment_id, "delivery:deployment:resolve")
        session = self.sc.session
        row = self._get_row(deployment_id, for_update=True)
        if row["status"] != "unknown_outcome":
            raise DeploymentNotUnknown("the deployment does not have an unknown outcome")
        pending = repository.deployment_jobs(
            session, deployment_id, ("reconcile",), tenant_id=self.sc.tenant_id
        )
        if any(j["status"] in ("queued", "leased") for j in pending):
            raise ReconcilePending("a reconcile of the deployment is already pending")
        job_id = gateway.enqueue_job(
            session,
            tenant_id=self.sc.tenant_id,
            kind="reconcile",
            plan_id=row["plan_id"],
            deployment_id=deployment_id,
            target_id=row["target_id"],
        )
        self._audit("delivery.deployment.reconcile_requested", row, job_id=str(job_id))
        session.commit()
        return self._out(row)

    def mark_failed(self, deployment_id: UUID, body: s.DeploymentMarkFailed) -> s.DeploymentOut:
        self._permitted(deployment_id, "delivery:deployment:resolve")
        session = self.sc.session
        row = self._get_row(deployment_id, for_update=True)
        if row["status"] != "unknown_outcome":
            raise DeploymentNotUnknown("the deployment does not have an unknown outcome")
        repository.resolve_failed(
            session,
            deployment_id,
            self.sc.principal.principal_id,
            body.note,
            tenant_id=self.sc.tenant_id,
        )
        resolved = self._get_row(deployment_id)
        self._audit("delivery.deployment.marked_failed", resolved, note=body.note)
        session.commit()
        return self._out(resolved)
