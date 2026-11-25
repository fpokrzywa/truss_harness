"""Deployment plans and their second-person decision (design sections 3 and 4, section 9 items 1,
2, 9, 11, 12 and 17; DECISIONS B-2675..B-2679).

A plan request builds the artifact here (offline: no credential, no outbound request): the SDK
template, or the generated app of a `/2` release. It stores it and queues one `plan` job for the
executor. The job's result seals the plan or fails it (`service.on_plan_result`). A sealed plan is
decided once, by a person other than its requester who holds `delivery:plan:approve` on the target's
scope, before `expires_at`; approve quotes the sealed digest. Related principals count as the
requester (B-2733): a service account the requester owns (`service_accounts.owner_principal_id`) and
the owner of a requesting service account are refused too, and so is a service account with the same
owner as a requesting service account (sibling service accounts, B-2911; an unowned account has no
siblings): 403 `self_approval_forbidden`, and that refusal is audited (`delivery.plan.approve` or
`delivery.plan.reject`, outcome `denied`, committed before the 403). The `delivery_approvals` insert
trigger enforces only the same-principal case. Approval creates the plan's deployment (`queued`) and
its one-attempt `apply` job, due until the plan's `expires_at`, in the approval's transaction
(B-2684). A target with an unresolved unknown outcome takes no new plan (409 `target_unresolved`,
settled call 10).

A plan deploys one sealed release (SP3 design sections 4 and 6, settled calls 3 to 6): the request
names a release the caller can read (else `release not found`), not withdrawn (409
`release_withdrawn`), in the target's workspace and project (409 `release_target_mismatch`), with a
passing evaluation of its digests (409 `evaluation_required`) and of the target's kind (409
`target_kind_mismatch`: the manifest's `target_kind` is not of the target's `platform_kind`). The
plan stores the release, its digest and the passing run; the artifact carries `release.json` and the
plan digest the release digest. Approval checks the release again after the plan row lock; reject
does not.

A `/2` release (compiler `packaging.service.COMPILER_VERSION_APP`, SP5 design 6.6) on a Databricks
target deploys the generated app instead of the SDK template. After the checks above it is refused
with 409, in this order and before any write: `serving_endpoint_required` (the target has no
`serving_endpoint` binding), `serving_endpoint_mismatch` (the release's single model binding's
`model` is not that endpoint) and `model_not_in_target_workspace` (the binding's provider is neither
an `openai_compatible` provider the requester can read whose `base_url` scheme and host normalize to
the target's address, nor, in development and test only, a `fake` provider; the `base_url` is only
compared, never stored). It then gathers `Resolved` under the requester's own scope
(`appgen.service.resolved_for_release`: 409 `release_source_changed` when a pinned version changed
or this organization cannot read it), renders the app (`appgen.render.render`: 409
`generator_version_unsupported`) and stores the archive as an artifact of the requester's
organization; the plan digest also covers the generator version. Any other release on a Databricks
target takes the template path unchanged.

A Snowflake target deploys only the generated Native App of a `/2` release (SP6 design 6.4), never
the template. After the checks above it is refused with 409, in this order and before any write:
`cortex_model_mismatch` (the release's single model binding's `model` is not the target's
`cortex_model`) and `model_not_in_target_account` (the same provider rule against the target's
account address, read from `Resolved.model.provider`; `Resolved` is gathered first, so a changed
pin's `release_source_changed` may come before it). The render then refuses with
`generator_version_unsupported` or `release_not_renderable` (a fixed detail: a pinned version may
be one the requester cannot see).

A plan is scoped by its target's workspace/project; a plan or target the caller cannot read gets
the answer an unknown id gets. Every plan route first settles the organization's plans (the lost
job sweep, lost plan jobs, passed expiries, then the deployment settle step) and commits that, so
a refused decision still leaves an expired plan `expired`. Approve and reject check
`delivery:plan:approve` before that settle, so a caller without it causes no write, then re-read
the plan under its row lock. The artifact's content is never returned on a user route, and a
plan's release fields (`release_id`, `release_digest`, `evaluation_run_id`) are returned only to
a caller who can read that release (else null).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any  # Any: plan and target rows
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from shifde.adapters.native.addresses import (
    AddressRefused,
    normalize_databricks_address,
    normalize_snowflake_address,
)
from shifde.adapters.native.base import create_archive
from shifde.adapters.native.contracts import DatabricksApplicationSpec
from shifde.adapters.native.databricks.source import scaffold_files
from shifde.adapters.runtime import IntegrationRuntime
from shifde.appgen.render import GeneratorVersionUnsupported, ResolvedProvider, render
from shifde.appgen.render_snowflake import ReleaseNotRenderable
from shifde.appgen.service import resolved_for_release
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
from shifde.models_admin.service import ModelService
from shifde.packaging.canonical import canonical
from shifde.packaging.schemas import TARGET_ADAPTERS
from shifde.packaging.service import READ, release_record, scoped_release
from shifde.tenancy.scope import TenantScope

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


class TargetKindMismatch(AppError):
    status, code, title = 409, "target_kind_mismatch", "Conflict"


class ServingEndpointRequired(AppError):
    status, code, title = 409, "serving_endpoint_required", "Conflict"


class ServingEndpointMismatch(AppError):
    status, code, title = 409, "serving_endpoint_mismatch", "Conflict"


class ModelNotInTargetWorkspace(AppError):
    status, code, title = 409, "model_not_in_target_workspace", "Conflict"


class CortexModelMismatch(AppError):
    status, code, title = 409, "cortex_model_mismatch", "Conflict"


class ModelNotInTargetAccount(AppError):
    status, code, title = 409, "model_not_in_target_account", "Conflict"


def check_target_kind(manifest: Mapping[str, Any], platform_kind: str) -> None:
    """A release is deployed only to the kind of target its manifest names."""
    kind = manifest.get("target_kind")
    if not isinstance(kind, str) or TARGET_ADAPTERS.get(kind) != platform_kind:
        raise TargetKindMismatch("The release is for another kind of target.")


def template_artifact(manifest: Mapping[str, Any], release_digest: str) -> tuple[bytes, str]:
    """The SDK template archive (default `databricks.app/v1` spec) with the release at its root
    (`release.json`, the canonical JSON of the manifest and its digest), and its sha256 digest."""
    files = scaffold_files(DatabricksApplicationSpec())
    files["release.json"] = canonical({"manifest": manifest, "digest": release_digest})
    return archive(files)


def archive(files: dict[str, bytes]) -> tuple[bytes, str]:
    """The artifact archive of `files` (at most 5 MB) and its sha256 digest."""
    content = create_archive(files)
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ValidationFailed("the artifact exceeds 5 MB")
    return content, hashlib.sha256(content).hexdigest()


def _constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


class PlanService:
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.runtime = runtime

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

    def _audit(
        self, action: str, row: Mapping[str, Any], *, outcome: str = "success", **details: Any
    ) -> None:
        audit.record(
            self.sc.session,
            self.sc.ctx,
            action=action,
            outcome=outcome,
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

    def readable(self, plan_id: UUID) -> bool:
        """Whether the caller may read the plan `plan_id` of this client (False for an unknown
        id and another client's alike), without the settle step: no commit."""
        row = repository.get_plan(self.sc.session, plan_id, tenant_id=self.sc.tenant_id)
        return bool(
            row is not None
            and row["tenant_id"] == self.sc.tenant_id
            and self.sc.pdp.authorize("delivery:plan:read", self._ref(row)).allowed
        )

    def request(self, body: s.PlanCreate) -> s.PlanOut:
        self._settle()
        target = TargetService(self.sc).plannable(body.target_id)
        if target["status"] != "active":
            raise gateway.TargetUnavailable("the target is archived")
        if repository.target_unresolved(self.sc.session, target["id"], tenant_id=self.sc.tenant_id):
            raise TargetUnresolved("the target has an unresolved unknown outcome")
        release, passing = self._gate(target, body.release_id)
        generator_version = service.app_generator_version(release["manifest"])
        if target["platform_kind"] == "snowflake":
            content, artifact_digest = self._snowflake_artifact(target, release)
        elif generator_version is None:
            content, artifact_digest = template_artifact(release["manifest"], release["digest"])
        else:
            content, artifact_digest = self._app_artifact(target, release)
        session = self.sc.session
        principal_id = self.sc.principal.principal_id
        artifact_id, plan_id = uuid7(), uuid7()
        repository.insert_artifact(
            session,
            {
                "tenant_id": self.sc.tenant_id,
                "id": artifact_id,
                "target_kind": release["manifest"]["target_kind"],
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
            **({"generator_version": generator_version} if generator_version else {}),
        )
        session.commit()
        return self._out(row)

    def _app_artifact(
        self, target: Mapping[str, Any], release: Mapping[str, Any]
    ) -> tuple[bytes, str]:
        """The generated app's archive and digest for a `/2` release (SP5 6.6): the serving
        endpoint and model checks, `Resolved` under the requester's scope, then render."""
        manifest = release["manifest"]
        endpoint = target["bindings"].get("serving_endpoint")
        if not endpoint:
            raise ServingEndpointRequired("The target has no serving endpoint binding.")
        bindings = list(((manifest.get("bindings") or {}).get("model_bindings") or {}).values())
        if len(bindings) != 1 or bindings[0].get("model") != endpoint:
            raise ServingEndpointMismatch(
                "The release's model binding is not the target's serving endpoint."
            )
        if not self._in_workspace(bindings[0].get("provider_id"), target["address"]):
            raise ModelNotInTargetWorkspace(
                "The release's model provider is not in the target's workspace."
            )
        resolved = resolved_for_release(self.sc, self.runtime, manifest)
        return archive(render(manifest, release["digest"], resolved))

    def _in_workspace(self, provider_id: Any, address: str) -> bool:  # Any: a JSON value
        """The provider is an `openai_compatible` provider the caller can read whose `base_url`
        (scheme and host) normalizes to `address`, or a `fake` one in development and test."""
        try:
            found = ModelService(self.sc, self.runtime).provider_destination(UUID(str(provider_id)))
        except ValueError:
            return False
        if found is None:
            return False
        adapter_key, base_url = found
        settings = self.runtime.settings
        if adapter_key == "fake":
            return settings.env in ("development", "test")
        if adapter_key != "openai_compatible" or base_url is None:
            return False
        try:
            parts = urlsplit(base_url)
            origin = normalize_databricks_address(
                f"{parts.scheme}://{parts.netloc}", settings.delivery_databricks_host_suffixes
            )
        except (AddressRefused, ValueError):
            return False
        return origin == address

    def _snowflake_artifact(
        self, target: Mapping[str, Any], release: Mapping[str, Any]
    ) -> tuple[bytes, str]:
        """The generated Native App's archive and digest (SP6 6.4): the Cortex model check,
        `Resolved` under the requester's scope, the provider check, then render."""
        manifest = release["manifest"]
        cortex_model = target["bindings"].get("cortex_model")
        bindings = list(((manifest.get("bindings") or {}).get("model_bindings") or {}).values())
        if not cortex_model or len(bindings) != 1 or bindings[0].get("model") != cortex_model:
            raise CortexModelMismatch(
                "The release's model binding is not the target's Cortex model."
            )
        resolved = resolved_for_release(self.sc, self.runtime, manifest)
        if not self._in_account(resolved.model.provider, target["address"]):
            raise ModelNotInTargetAccount(
                "The release's model provider is not in the target's account."
            )
        if service.app_generator_version(manifest) is None:
            raise GeneratorVersionUnsupported("the release is not a generated app release")
        try:
            files = render(manifest, release["digest"], resolved)
        except ReleaseNotRenderable:
            # the render's detail names a tool and its SQL, which the requester may not see
            raise ReleaseNotRenderable("The release cannot be rendered as a Native App.") from None
        return archive(files)

    def _in_account(self, provider: ResolvedProvider | None, address: str) -> bool:
        """The provider is an `openai_compatible` one whose `base_url` (scheme and host)
        normalizes to exactly the Snowflake `address`, or a `fake` one in development and test."""
        if provider is None:
            return False
        if provider.kind == "fake":
            return self.runtime.settings.env in ("development", "test")
        if provider.kind != "openai_compatible" or provider.base_url is None:
            return False
        try:
            parts = urlsplit(provider.base_url)
            origin = normalize_snowflake_address(f"{parts.scheme}://{parts.netloc}")
        except (AddressRefused, ValueError):
            return False
        return origin == address

    def _gate(
        self, target: Mapping[str, Any], release_id: UUID
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """(release, passing evaluation run) the request may deploy to `target`: a release the
        caller can read (else `release not found`), still sealed, in the target's scope, with a
        passing evaluation of its digests (settled calls 3, 4 and 8) and of the target's kind."""
        release = scoped_release(self.sc, release_id)
        if release["status"] == "withdrawn":
            raise ReleaseWithdrawn("The release is withdrawn.")
        if release["workspace_id"] != target["workspace_id"] or (
            target["project_id"] is not None and release["project_id"] != target["project_id"]
        ):
            raise ReleaseTargetMismatch("The release is not in the target's workspace and project.")
        passed = require_passing_evaluation(self.sc.session, release_id, ctx=self.sc.ctx)
        check_target_kind(passed[0]["manifest"], target["platform_kind"])
        return passed

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

    def _related(self, requested_by: UUID) -> str | None:
        """Why the caller may not decide a plan `requested_by` requested (None: they may): the
        same principal, a service account the requester owns, the owner of the requesting
        service account, or a sibling service account (both owned by the same principal; an
        unowned account has no siblings)."""
        principal = self.sc.principal
        if requested_by == principal.principal_id:
            return "same_principal"
        session, tenant_id = self.sc.session, self.sc.tenant_id
        decider_owner = (
            repository.service_account_owner(session, principal.principal_id, tenant_id=tenant_id)
            if principal.principal_type == "service_account"
            else None
        )
        if decider_owner == requested_by:
            return "decider_owned_by_requester"
        requester_owner = repository.service_account_owner(
            session, requested_by, tenant_id=tenant_id
        )
        if requester_owner == principal.principal_id:
            return "requester_owned_by_decider"
        if decider_owner is not None and requester_owner == decider_owner:
            return "sibling_service_accounts"
        return None

    def approve(self, plan_id: UUID, body: s.PlanApprove) -> s.PlanOut:
        return self._decide(plan_id, "approve", body.note, body.plan_digest)

    def reject(self, plan_id: UUID, body: s.PlanReject) -> s.PlanOut:
        return self._decide(plan_id, "reject", body.note, None)

    def _decide(
        self, plan_id: UUID, decision: str, note: str | None, quoted_digest: str | None
    ) -> s.PlanOut:
        # the permission first: a caller who may not decide causes no settle commit
        self._require("delivery:plan:approve", self._get_row(plan_id))
        self._settle()
        session = self.sc.session
        row = self._get_row(plan_id, for_update=True)
        principal_id = self.sc.principal.principal_id
        reason = self._related(row["requested_by"])
        if reason is not None:
            self._audit(
                f"delivery.plan.{decision}", row, outcome="denied", decision=decision, reason=reason
            )
            session.commit()
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
