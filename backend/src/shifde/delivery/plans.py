"""Deployment plans and their second-person decision (design sections 3 and 4, section 9 items 1,
2, 9, 11, 12 and 17; DECISIONS B-2675..B-2679).

A plan request builds the SDK template artifact here (offline: no credential, no outbound
request), stores it, and queues one `plan` job for the executor. The job's result seals the plan
or fails it (`service.on_plan_result`). A sealed plan is decided once, by a person other than its
requester who holds `delivery:plan:approve` on the target's scope, before `expires_at`; approve
quotes the sealed digest. The `delivery_approvals` insert trigger enforces the same rule.
Approval creates the plan's deployment (`queued`) and its one-attempt `apply` job, due until the
plan's `expires_at`, in the approval's transaction (B-2684). A target with an unresolved unknown
outcome takes no new plan (409 `target_unresolved`, settled call 10).

A plan deploys one sealed release (SP3 design sections 4 and 6, settled calls 3 to 6): the request
names a release the caller can read (else `release not found`), not withdrawn (409
`release_withdrawn`), of the target's kind and in its workspace and project (409
`release_target_mismatch`), with a passing evaluation of its digests (409 `evaluation_required`).
The plan stores the release, its digest and the passing run; the artifact carries `release.json`
and the plan digest the release digest. Approval checks the release again after the plan row
lock; reject does not.

A plan is scoped by its target's workspace/project; a plan or target the caller cannot read gets
the answer an unknown id gets. Every plan route first settles the organization's plans (the lost
job sweep, lost plan jobs, passed expiries, then the deployment settle step) and commits that, so
a refused decision still leaves an expired plan `expired`. The artifact's content is never
returned on a user route, and a plan's release fields (`release_id`, `release_digest`,
`evaluation_run_id`) are returned only to a caller who can read that release (else null).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any  # Any: plan and target rows
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from shifde.adapters.native.base import create_archive
from shifde.adapters.native.contracts import DatabricksApplicationSpec
from shifde.adapters.native.databricks.source import scaffold_files
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.core.errors import AppError, NotFound, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.delivery import gateway, repository, service
from shifde.delivery import schemas as s
from shifde.delivery.service import TargetService
from shifde.evaluation.service import (
    EvaluationRequired,
    ReleaseWithdrawn,
    require_passing_evaluation,
)
from shifde.packaging.canonical import canonical
from shifde.packaging.service import READ, release_record, scoped_release
from shifde.tenancy.scope import TenantScope

TARGET_KIND = "databricks.app/v1"
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024  # 5 242 880, also the table CHECK
APPROVAL_CONSTRAINT = "uq_delivery_approvals__tenant_plan"


class SelfApprovalForbidden(AppError):
    status, code, title = 403, "self_approval_forbidden", "Forbidden"


class PlanExpired(AppError):
    status, code, title = 409, "plan_expired", "Conflict"


class PlanNotSealed(AppError):
    status, code, title = 409, "plan_not_sealed", "Conflict"


class PlanAlreadyDecided(AppError):
    status, code, title = 409, "plan_already_decided", "Conflict"


class PlanDigestMismatch(AppError):
    status, code, title = 409, "plan_digest_mismatch", "Conflict"


class TargetUnresolved(AppError):
    status, code, title = 409, "target_unresolved", "Conflict"


class ReleaseTargetMismatch(AppError):
    status, code, title = 409, "release_target_mismatch", "Conflict"


def check_target_kind(manifest: Mapping[str, Any]) -> None:
    """A release is deployed only to the kind of target its manifest names."""
    if manifest.get("target_kind") != TARGET_KIND:
        raise ReleaseTargetMismatch("The release is for another kind of target.")


def template_artifact(manifest: Mapping[str, Any], release_digest: str) -> tuple[bytes, str]:
    """The SDK template archive (default `databricks.app/v1` spec) with the release at its root
    (`release.json`, the canonical JSON of the manifest and its digest), and its sha256 digest."""
    files = scaffold_files(DatabricksApplicationSpec())
    files["release.json"] = canonical({"manifest": manifest, "digest": release_digest})
    content = create_archive(files)
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ValidationFailed("the artifact exceeds 5 MB")
    return content, hashlib.sha256(content).hexdigest()


def _constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


class PlanService:
    def __init__(self, scope: TenantScope) -> None:
        self.sc = scope

    # ================================================================== helpers
    def _settle(self) -> None:
        """The lost-job sweep and the plan and deployment settle steps, committed before the
        route goes on."""
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
            resource_type="delivery_plan",
            resource_id=row["id"],
        )

    def _get_row(self, plan_id: UUID, *, for_update: bool = False) -> Mapping[str, Any]:
        row = repository.get_plan(
            self.sc.session, plan_id, tenant_id=self.sc.tenant_id, for_update=for_update
        )
        if row is None or row["tenant_id"] != self.sc.tenant_id:
            raise NotFound("plan not found")
        return row

    def _require(self, action: str, row: Mapping[str, Any]) -> None:
        try:
            self.sc.pdp.require(action, self._ref(row), hide_unless="delivery:plan:read")
        except NotFound as exc:
            # a plan the caller cannot read gets the answer an unknown id gets
            raise NotFound("plan not found") from exc

    def _audit(self, action: str, row: Mapping[str, Any], **details: Any) -> None:
        audit.record(
            self.sc.session,
            self.sc.ctx,
            action=action,
            tenant_id=self.sc.tenant_id,
            target_type="delivery_plan",
            target_id=row["id"],
            target_label=f"delivery_plan:{row['application_key']}",
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            details={
                "plan_id": str(row["id"]),
                "target_id": str(row["target_id"]),
                "application_key": row["application_key"],
                **details,
            },
        )

    def _release_visible(self, row: Mapping[str, Any]) -> bool:
        """The caller may read the plan's release (`packaging:solution:read` on its solution's
        scope, the ref `scoped_release` checks). `authorize`: no raise, no decision record."""
        if row["release_id"] is None:
            return False
        rel = release_record(self.sc.session, row["release_id"], tenant_id=self.sc.tenant_id)
        if rel is None:
            return False
        ref = ResourceRef(
            self.sc.tenant_id,
            rel["workspace_id"],
            rel["project_id"],
            resource_type="packaging_release",
            resource_id=rel["id"],
        )
        return self.sc.pdp.authorize(READ, ref).allowed

    def _out(self, row: Mapping[str, Any], visible: bool | None = None) -> s.PlanOut:
        """The plan; its release fields null unless the caller can read the release (`visible`,
        else checked here)."""
        if visible is None:
            visible = self._release_visible(row)
        approval = (
            s.PlanApprovalOut(
                decision=row["approval_decision"],
                decided_by=row["approval_decided_by"],
                decided_at=row["approval_decided_at"],
                note=row["approval_note"],
            )
            if row["approval_decision"] is not None
            else None
        )
        return s.PlanOut(
            id=row["id"],
            target_id=row["target_id"],
            application_key=row["application_key"],
            display_name=row["display_name"],
            status=row["status"],
            status_reason=row["status_reason"],
            requested_by=row["requested_by"],
            artifact_id=row["artifact_id"],
            artifact_digest=row["artifact_digest"],
            target_revision=row["target_revision"],
            steps=row["steps"],
            snapshot=row["snapshot"],
            adapter_version=row["adapter_version"],
            toolchain_digest=row["toolchain_digest"],
            digest=row["digest"],
            sealed_at=row["sealed_at"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approval=approval,
            deployment_id=row["deployment_id"],
            release_id=row["release_id"] if visible else None,
            release_digest=row["release_digest"] if visible else None,
            evaluation_run_id=row["evaluation_run_id"] if visible else None,
        )

    # ================================================================== routes
    def list(
        self, *, status: str | None, target: UUID | None, limit: int, cursor: str | None
    ) -> Page[s.PlanOut]:
        self._settle()
        pdp = self.sc.pdp
        flt = pdp.visible_scopes("delivery:plan:read")
        if not (flt.tenant_wide or flt.workspace_ids or flt.project_ids):
            pdp.require("delivery:plan:read", self.sc.tenant_ref)  # 403 + decision record
        rows = repository.list_plans(
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
        visible: dict[UUID, bool] = {}  # one release lookup per distinct release on the page
        for r in page:
            if r["release_id"] is not None and r["release_id"] not in visible:
                visible[r["release_id"]] = self._release_visible(r)
        return Page(
            items=[self._out(r, visible.get(r["release_id"], False)) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, plan_id: UUID) -> s.PlanOut:
        self._settle()
        row = self._get_row(plan_id)
        self._require("delivery:plan:read", row)
        return self._out(row)

    def request(self, body: s.PlanCreate) -> s.PlanOut:
        self._settle()
        target = TargetService(self.sc).plannable(body.target_id)
        if target["status"] != "active":
            raise gateway.TargetUnavailable("the target is archived")
        if repository.target_unresolved(self.sc.session, target["id"], tenant_id=self.sc.tenant_id):
            raise TargetUnresolved("the target has an unresolved unknown outcome")
        release, passing = self._gate(target, body.release_id)
        content, artifact_digest = template_artifact(release["manifest"], release["digest"])
        session = self.sc.session
        principal_id = self.sc.principal.principal_id
        artifact_id, plan_id = uuid7(), uuid7()
        repository.insert_artifact(
            session,
            {
                "tenant_id": self.sc.tenant_id,
                "id": artifact_id,
                "target_kind": TARGET_KIND,
                "digest": artifact_digest,
                "size": len(content),
                "content": content,
                "created_by": principal_id,
            },
        )
        repository.insert_plan(
            session,
            {
                "tenant_id": self.sc.tenant_id,
                "id": plan_id,
                "target_id": target["id"],
                "target_revision": target["revision"],
                "artifact_id": artifact_id,
                "artifact_digest": artifact_digest,
                "application_key": body.application_key,
                "display_name": body.display_name,
                "requested_by": principal_id,
                "release_id": release["id"],
                "release_digest": release["digest"],
                "evaluation_run_id": passing["id"],
            },
        )
        job_id = gateway.enqueue_job(
            session,
            tenant_id=self.sc.tenant_id,
            kind="plan",
            plan_id=plan_id,
            target_id=target["id"],
        )
        row = self._get_row(plan_id)
        self._audit(
            "delivery.plan.requested",
            row,
            artifact_id=str(artifact_id),
            artifact_digest=artifact_digest,
            target_revision=target["revision"],
            job_id=str(job_id),
            release_id=str(release["id"]),
            release_digest=release["digest"],
            evaluation_run_id=str(passing["id"]),
        )
        session.commit()
        return self._out(row)

    def _gate(
        self, target: Mapping[str, Any], release_id: UUID
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """(release, passing evaluation run) the request may deploy to `target`: a release the
        caller can read (else `release not found`), still sealed, of the target's kind and
        scope, with a passing evaluation of its digests (settled calls 3, 4 and 8)."""
        release = scoped_release(self.sc, release_id)
        if release["status"] == "withdrawn":
            raise ReleaseWithdrawn("The release is withdrawn.")
        check_target_kind(release["manifest"])
        if release["workspace_id"] != target["workspace_id"] or (
            target["project_id"] is not None and release["project_id"] != target["project_id"]
        ):
            raise ReleaseTargetMismatch("The release is not in the target's workspace and project.")
        return require_passing_evaluation(self.sc.session, release_id, ctx=self.sc.ctx)

    def _recheck(self, row: Mapping[str, Any]) -> None:
        """At approval, under the plan row lock: the plan's release is still sealed, unchanged
        and passed. No packaging permission is asked of the approver."""
        session = self.sc.session
        if row["release_id"] is None:
            raise EvaluationRequired("This release has no passing evaluation.")
        release = release_record(session, row["release_id"], tenant_id=self.sc.tenant_id)
        if release is not None and release["status"] == "withdrawn":
            raise ReleaseWithdrawn("The release is withdrawn.")
        try:
            current, _ = require_passing_evaluation(session, row["release_id"], ctx=self.sc.ctx)
        except (NotFound, EvaluationRequired):
            raise EvaluationRequired("This release has no passing evaluation.") from None
        if current["digest"] != row["release_digest"]:
            raise EvaluationRequired("This release has no passing evaluation.")

    def approve(self, plan_id: UUID, body: s.PlanApprove) -> s.PlanOut:
        return self._decide(plan_id, "approve", body.note, body.plan_digest)

    def reject(self, plan_id: UUID, body: s.PlanReject) -> s.PlanOut:
        return self._decide(plan_id, "reject", body.note, None)

    def _decide(
        self, plan_id: UUID, decision: str, note: str | None, quoted_digest: str | None
    ) -> s.PlanOut:
        self._settle()
        session = self.sc.session
        row = self._get_row(plan_id, for_update=True)
        self._require("delivery:plan:approve", row)
        principal_id = self.sc.principal.principal_id
        if row["requested_by"] == principal_id:
            raise SelfApprovalForbidden("a plan is decided by a person other than its requester")
        if row["status"] == "sealed" and row["db_now"] >= row["expires_at"]:
            repository.decide_plan(session, plan_id, "expired", tenant_id=self.sc.tenant_id)
            session.commit()
            raise PlanExpired("the plan has expired")
        if row["status"] == "expired":
            raise PlanExpired("the plan has expired")
        if row["status"] in ("approved", "rejected"):
            raise PlanAlreadyDecided("the plan is already decided")
        if row["status"] != "sealed":
            raise PlanNotSealed("the plan is not sealed")
        if quoted_digest is not None and quoted_digest != row["digest"]:
            raise PlanDigestMismatch("plan_digest is not the sealed digest")
        if decision == "approve":
            try:
                self._recheck(row)
            except AppError:
                session.rollback()  # the plan stays sealed; nothing is written
                raise
        try:
            with session.begin_nested():
                repository.insert_approval(
                    session,
                    {
                        "tenant_id": self.sc.tenant_id,
                        "id": uuid7(),
                        "plan_id": plan_id,
                        "plan_digest": row["digest"],
                        "decided_by": principal_id,
                        "decision": decision,
                        "note": note,
                    },
                )
        except IntegrityError as exc:
            if _constraint(exc) == APPROVAL_CONSTRAINT:
                raise PlanAlreadyDecided("the plan is already decided") from None
            raise
        status = "approved" if decision == "approve" else "rejected"
        repository.decide_plan(session, plan_id, status, tenant_id=self.sc.tenant_id)
        created: dict[str, str] = {}
        if decision == "approve":
            deployment_id = uuid7()
            repository.insert_deployment(
                session,
                {
                    "tenant_id": self.sc.tenant_id,
                    "id": deployment_id,
                    "plan_id": plan_id,
                    "target_id": row["target_id"],
                    "application_key": row["application_key"],
                },
            )
            job_id = gateway.enqueue_job(
                session,
                tenant_id=self.sc.tenant_id,
                kind="apply",
                plan_id=plan_id,
                deployment_id=deployment_id,
                target_id=row["target_id"],
                deadline=row["expires_at"],
            )
            created = {"deployment_id": str(deployment_id), "apply_job_id": str(job_id)}
        decided = self._get_row(plan_id)
        self._audit(
            f"delivery.plan.{status}",
            decided,
            digest=row["digest"],
            decision=decision,
            **created,
        )
        session.commit()
        return self._out(decided)
