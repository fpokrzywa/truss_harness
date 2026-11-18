"""Run and approval use cases of the control plane (the API validates, persists and enqueues; the
worker executes). Contract: docs/api/PHASE_5_CONTRACT.md sections 2-3 and amendments D1.x.

Authorization: `agent:execute` / `orchestration:execute` at the target's scope to start, `run:read`
for visibility (scope filtered, anything else 404), `run:cancel` (or being the initiator) to
cancel, `approval:read` / `approval:decide` for the queue, with separation of duties unless the
tenant setting `allow_self_approval` is true. Pre-flight authorization / validation failures
create NO run (no existence oracle); a budget denial creates a `failed` run (auditable).

Ported from SETH 3ba0e06 (RT-11, B-2270..). Every repository call names the caller's client
(`self.sc.tenant_id`, the request's resolved organization, never a body or a row field), so
another client's run, approval or artifact id answers exactly like an unknown id; a run is never
started in an archived workspace or project (findings Item 12, REQ F-02); a draft pipeline test
run records the draft's resolved pin set next to its spec snapshot (B-2195).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any  # Any: stored documents / audit details
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.build.issues import schema_errors
from shifde.build.kind_ops import AgentOps
from shifde.build.service import ArtifactService, issues_as_errors
from shifde.core.clock import utcnow
from shifde.core.errors import (
    AlreadyDecided,
    AppError,
    Conflict,
    Forbidden,
    IdempotencyConflict,
    NotFound,
    ScopeArchived,
    SeparationOfDuties,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.redaction import scrub
from shifde.observability import tracing
from shifde.runtime import repository as repo
from shifde.runtime import schemas as s
from shifde.runtime import state
from shifde.runtime.governance import BudgetDenied, BudgetScope, GovernanceHooks
from shifde.runtime.settle import cancel_open_children, scrub_ended, settle_terminal
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

INDETERMINATE_DECISIONS = ("mark_succeeded", "retry", "fail")
NORMAL_DECISIONS = ("approve", "reject")
PREVIEW_BYTES = 4096
MAX_PARAMS_BYTES = 200_000
ARTIFACT_BYTES = 1_000_000
STATUS_GROUPS = {
    "pending": ["pending"],
    "decided": ["approved", "rejected"],
    "expired": ["expired", "cancelled"],
}


def _scope_tuple(pdp_scope: Any) -> tuple[bool, list[UUID], list[UUID]]:  # Any: ScopeFilter
    return pdp_scope.tenant_wide, list(pdp_scope.workspace_ids), list(pdp_scope.project_ids)


class RunService:
    def __init__(
        self, scope: TenantScope, runtime: IntegrationRuntime, hooks: GovernanceHooks
    ) -> None:
        self.sc = scope
        self.runtime = runtime
        self.hooks = hooks
        self.artifacts = ArtifactService(scope, runtime)

    @property
    def session(self) -> Session:
        return self.sc.session

    # ================================================================== helpers
    def _active_tenant(self) -> None:
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    def _ref(self, row: Mapping[str, Any]) -> ResourceRef:  # Any: run / approval row
        return ResourceRef(self.sc.tenant_id, row["workspace_id"], row["project_id"])

    def _audit(
        self,
        action: str,
        run: Mapping[str, Any],  # Any: run row
        details: Mapping[str, Any],  # Any: audit details
        *,
        outcome: str = "success",
    ) -> None:
        audit.record(
            self.session,
            self.sc.ctx,
            action=action,
            outcome=outcome,
            tenant_id=self.sc.tenant_id,
            target_type="run",
            target_id=run["id"],
            target_label=f"run:{run['id']}",
            workspace_id=run["workspace_id"],
            project_id=run["project_id"],
            details={"run_id": str(run["id"]), "kind": run["kind"], **dict(details)},
        )

    def _active_scope(self, workspace_id: UUID, project_id: UUID) -> None:
        """Findings Item 12 (REQ F-02): an archived workspace or project starts no run (archived
        scopes are read-only, as for connections, B-1509). The ids are of this client (resolved
        from the caller's slugs or the target's own scope)."""
        ws_status, proj_status = repo.scope_statuses(
            self.session, self.sc.tenant_id, workspace_id, project_id
        )
        if ws_status != "active":
            raise ScopeArchived("workspace is archived")
        if proj_status != "active":
            raise ScopeArchived("project is archived")

    def _own_run(self, run_id: UUID, *, lock: bool = False) -> Mapping[str, Any] | None:  # Any
        """The run `run_id` of the caller's client, else None (another client's id answers as an
        unknown one)."""
        tenant = self.sc.tenant_id
        row = (
            repo.lock_run(self.session, tenant, run_id)
            if lock
            else repo.get_run(self.session, tenant, run_id)
        )
        return row if row is not None and row["tenant_id"] == tenant else None

    def _readable_run(self, run_id: UUID) -> Mapping[str, Any]:  # Any: run row
        row = self._own_run(run_id)
        if row is None or not self.sc.pdp.authorize("run:read", self._ref(row)).allowed:
            raise NotFound("run not found")
        return row

    # ================================================================== outputs
    def _runs_out(self, rows: list[Mapping[str, Any]]) -> list[s.RunOut]:  # Any: run rows
        tenant = self.sc.tenant_id
        totals = repo.run_totals(
            self.session, tenant, [r["id"] for r in rows if r["parent_run_id"] is None]
        )
        unknown = repo.unknown_outcome_runs(self.session, tenant, [r["id"] for r in rows])
        ws_slug, proj_slug = tenancy.scope_slug_map(
            self.sc, [r["workspace_id"] for r in rows], [r["project_id"] for r in rows]
        )
        names: dict[tuple[str, UUID], str] = {}
        out: list[s.RunOut] = []
        for r in rows:
            who = (r["principal_type"], r["initiated_by_principal_id"])
            if who not in names:
                names[who] = repo.principal_display(self.session, tenant, *who)
            total = totals.get(r["id"])
            usage = s.UsageOut(
                input_tokens=int(total["input_tokens"] if total else r["input_tokens"]),
                output_tokens=int(total["output_tokens"] if total else r["output_tokens"]),
                cached_tokens=int(total["cached_tokens"] if total else r["cached_tokens"]),
                model_calls=int(total["model_calls"] if total else r["model_calls"]),
                tool_calls=int(total["tool_calls"] if total else r["tool_calls"]),
            )
            out.append(
                s.RunOut(
                    id=r["id"],
                    kind=r["kind"],
                    status=r["status"],
                    tenant_id=r["tenant_id"],
                    workspace=s.ScopeOut(
                        id=r["workspace_id"], slug=ws_slug.get(r["workspace_id"], "")
                    ),
                    project=s.ScopeOut(id=r["project_id"], slug=proj_slug.get(r["project_id"], "")),
                    initiated_by=s.PrincipalOut(
                        type=r["principal_type"],
                        id=r["initiated_by_principal_id"],
                        display=names[who],
                    ),
                    agent_id=r["agent_id"],
                    agent_version_id=r["agent_version_id"],
                    prompt_version_id=r["prompt_version_id"],
                    orchestration_id=r["orchestration_id"],
                    orchestration_version_id=r["orchestration_version_id"],
                    pipeline_id=r["pipeline_id"],
                    pipeline_version_id=r["pipeline_version_id"],
                    ontology_id=r["ontology_id"],
                    ontology_version_id=r["ontology_version_id"],
                    action_key=r["action_key"],
                    parent_run_id=r["parent_run_id"],
                    root_run_id=r["root_run_id"],
                    node_id=r["node_id"],
                    is_test=r["is_test"],
                    trace_id=r["trace_id"],
                    correlation_id=r["correlation_id"],
                    idempotency_key=r["idempotency_key"],
                    created_at=r["created_at"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                    deadline_at=r["deadline_at"],
                    error_class=r["error_class"],
                    error_message=r["error_message"],
                    attempt_count=int(r["attempt_count"]),
                    usage=usage,
                    waiting_reason=r["waiting_reason"],
                    cancel_requested_at=r["cancel_requested_at"],
                    outcome_unknown=r["id"] in unknown,
                )
            )
        return out

    def run_out(self, row: Mapping[str, Any]) -> s.RunOut:  # Any: run row
        return self._runs_out([row])[0]

    # ================================================================== create
    def create(self, body: s.RunCreate, idempotency_key: str | None) -> tuple[s.RunOut, bool]:
        """(run, created). `created` is False for an idempotent replay."""
        self._active_tenant()
        principal = self.sc.principal
        fingerprint = hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        if idempotency_key is not None:
            existing = repo.run_by_idempotency(
                self.session, self.sc.tenant_id, principal.principal_id, idempotency_key
            )
            if existing is not None:
                return self._replay(existing, fingerprint), False

        ref = tenancy.scope_ref_from_slugs(self.sc, body.workspace, body.project)
        if ref.project_id is None or ref.workspace_id is None:
            raise ValidationFailed("a workspace and a project are required")
        self._active_scope(ref.workspace_id, ref.project_id)
        if body.kind == "pipeline":
            art, version, spec, payload, draft_run = self._pipeline_target(body, ref)
        else:
            art, version, spec = self._target(body, ref, principal)
            payload = {"input": body.input, "variables": body.variables}
            draft_run = False
        if body.kind == "agent":
            allowed = (spec.get("execution_permissions") or {}).get("allowed_principal_types")
            if allowed and principal.principal_type not in allowed:
                raise Forbidden("this agent does not allow this principal type")
            # dry-run the context assembly: unknown variables / bad input mapping are a 422 here
            try:  # the caller's env (no `run_of`: no run exists yet; carry_forward RT-7)
                AgentOps(self.artifacts).assemble(art, spec, body.input, body.variables)
            except AppError as exc:
                if isinstance(exc, ValidationFailed):
                    raise
                raise ValidationFailed(exc.detail) from exc
        if body.kind == "pipeline":
            deadline_s = int(spec["policies"]["deadline_seconds"])
        else:
            deadline_s = int((spec.get("limits") or {}).get("deadline_s") or 3600)
        deadline_s = min(deadline_s, self.runtime.settings.run_default_deadline_seconds * 24)
        now = utcnow()
        # the budget pre-flight of a run not yet stored (no usage can carry this id); the run's
        # own id is the repository's (B-2115)
        pending_id = uuid7()
        values: dict[str, Any] = {  # Any: column values
            "tenant_id": self.sc.tenant_id,
            "workspace_id": ref.workspace_id,
            "project_id": ref.project_id,
            "kind": body.kind,
            "initiated_by_principal_id": principal.principal_id,
            "principal_type": principal.principal_type,
            "api_key_id": principal.api_key_id,
            "idempotency_key": idempotency_key,
            "request_hash": fingerprint,
            "trace_id": tracing.current_trace_id() or uuid7().hex,
            "correlation_id": self.sc.ctx.request_id,
            "deadline_at": now + timedelta(seconds=deadline_s),
            "is_test": draft_run or body.is_test,
        }
        if body.kind == "agent":
            values.update(
                agent_id=art["id"],
                agent_version_id=version["id"],
                prompt_version_id=UUID(str(spec["prompt_version_id"])),
            )
        elif body.kind == "pipeline":
            values.update(pipeline_id=art["id"], pipeline_version_id=version["id"])
        else:
            values.update(orchestration_id=art["id"], orchestration_version_id=version["id"])
        try:
            self.hooks.preflight(
                self.session,
                BudgetScope(
                    self.sc.tenant_id,
                    ref.workspace_id,
                    ref.project_id,
                    values.get("agent_id"),
                    pending_id,
                    values.get("pipeline_id"),
                    principal.principal_id,
                ),
            )
        except BudgetDenied as exc:
            self.session.rollback()
            return self._budget_denied(values, payload, exc), True
        try:
            row = repo.create_run(self.session, values, payload)
            repo.enqueue(self.session, self.sc.tenant_id, row["id"])
            self._audit(
                "run.created",
                row,
                {"target_version_id": str(version["id"]), "idempotency_key": idempotency_key},
            )
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            existing = (
                repo.run_by_idempotency(
                    self.session, self.sc.tenant_id, principal.principal_id, idempotency_key
                )
                if idempotency_key
                else None
            )
            if existing is None:
                raise Conflict("the run could not be created") from exc
            return self._replay(existing, fingerprint), False
        return self.run_out(row), True

    def _target(
        self,
        body: s.RunCreate,
        ref: ResourceRef,
        principal: Any,  # Any: Principal
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], dict[str, Any]]:  # Any: rows / specs
        kind_plural = "agents" if body.kind == "agent" else "orchestrations"
        single = "agent" if body.kind == "agent" else "orchestration"
        target_id = body.agent_id if body.kind == "agent" else body.orchestration_id
        assert target_id is not None
        art = self.artifacts.artifact(kind_plural, target_id)  # 404 when not visible
        self.sc.pdp.require(f"{single}:execute", self.artifacts.ref_of(art))
        if art["status"] != "active":
            raise Conflict(f"the {single} is {art['status']}; only an active {single} can run")
        if art["workspace_id"] not in (None, ref.workspace_id) or art["project_id"] not in (
            None,
            ref.project_id,
        ):
            raise ValidationFailed(f"the {single} does not belong to this workspace / project")
        assert body.version_id != "draft"
        version = self._version(art, body.version_id)
        return art, version, dict(version["spec"])

    def _pipeline_target(
        self, body: s.RunCreate, ref: ResourceRef
    ) -> tuple[
        Mapping[str, Any], Mapping[str, Any], dict[str, Any], dict[str, Any], bool
    ]:  # Any: rows / specs
        """Authorize and validate a pipeline run request: (artifact, version row, validated spec,
        run input payload, is_draft_run). Nothing is created for a request that fails here."""
        assert body.pipeline_id is not None
        art = self.artifacts.artifact("pipelines", body.pipeline_id)  # 404 when not visible
        draft_run = body.version_id == "draft"
        self.sc.pdp.require(
            "pipeline:test" if draft_run else "pipeline:execute", self.artifacts.ref_of(art)
        )
        if art["status"] != "active":
            raise Conflict(f"the pipeline is {art['status']}; only an active pipeline can run")
        if art["workspace_id"] not in (None, ref.workspace_id) or art["project_id"] not in (
            None,
            ref.project_id,
        ):
            raise ValidationFailed("the pipeline does not belong to this workspace / project")
        if draft_run:
            draft = self.artifacts.draft_of(art)
            if draft is None:
                raise ValidationFailed("this pipeline has no draft to test")
            version: Mapping[str, Any] = draft
        else:
            assert body.version_id != "draft"
            version = self._version(art, body.version_id)
        # a draft is validated now; a published version is re-validated against today's world
        # (retired references, inactive connections, permissions of THIS caller) before it runs
        prepared = self.artifacts.prepare(art, dict(version["spec"]), resolve=draft_run)
        if prepared.report.errors:
            raise ValidationFailed(
                "the pipeline is invalid and cannot run",
                errors=issues_as_errors(prepared.report.errors),
            )
        spec = prepared.spec
        if len(json.dumps(body.params)) > MAX_PARAMS_BYTES:
            raise ValidationFailed(f"params are larger than {MAX_PARAMS_BYTES} bytes")
        problems = schema_errors(spec["params"], body.params)
        if problems:
            raise ValidationFailed(
                "the run parameters are invalid",
                errors=[
                    {
                        "loc": ["body", "params", *[str(p) for p in path]],
                        "msg": message,
                        "type": "invalid_params",
                    }
                    for path, message in problems
                ],
            )
        payload: dict[str, Any] = {"params": body.params}
        if draft_run:  # the draft is mutable: the run executes the spec it started with
            payload["spec_snapshot"] = spec
            # and the versions that spec resolved to, checked for this caller just above: a draft
            # has no pins of its own (they are written at publish), so the run's env answers
            # only for these and what they pin (B-2195)
            payload["pins"] = sorted({str(vid) for vid, _ in prepared.pins})
        return art, version, spec, payload, draft_run

    def _replay(self, existing: Mapping[str, Any], fingerprint: str) -> s.RunOut:  # Any: run row
        """An idempotent replay answers only what a fresh request would be allowed to see: the
        same request body, the caller may still read the run and still execute its target."""
        if existing["request_hash"] != fingerprint or existing["kind"] not in (
            "agent",
            "orchestration",
            "pipeline",
        ):
            raise IdempotencyConflict("this Idempotency-Key was used with a different request")
        if not self.sc.pdp.authorize("run:read", self._ref(existing)).allowed:
            raise NotFound("run not found")
        single = existing["kind"]
        target = existing[f"{single}_id"]
        art = self.artifacts.artifact(f"{single}s", target)  # 404 when no longer visible
        action = "execute"
        if existing["kind"] == "pipeline":  # a draft test run needs pipeline:test
            ran = self.artifacts.version_of(art, existing["pipeline_version_id"])
            action = "test" if ran["state"] == "draft" else "execute"
        self.sc.pdp.require(f"{single}:{action}", self.artifacts.ref_of(art))
        return self.run_out(existing)

    def _version(self, art: Mapping[str, Any], version_id: UUID | None) -> Mapping[str, Any]:  # Any
        if version_id is None:
            latest = art["latest_published_version_id"]
            if latest is None:
                raise ValidationFailed("there is no published version to run")
            version_id = latest
        version = self.artifacts.version_of(art, version_id)
        if version["state"] not in ("published", "deprecated"):
            raise ValidationFailed("only published versions can run")
        return version

    def _budget_denied(
        self,
        values: dict[str, Any],
        payload: dict[str, Any],
        exc: BudgetDenied,  # Any: columns
    ) -> s.RunOut:
        created = repo.create_run(self.session, values, payload)
        row = repo.transition(  # pending -> failed: the documented pre-flight rejection
            self.session,
            self.sc.tenant_id,
            created["id"],
            state.PENDING,
            state.FAILED,
            finished_at=utcnow(),
            error_class=state.BUDGET_DENIED,
            error_message=exc.message,
        )
        assert row is not None
        scrub_ended(self.session, row, self.runtime.settings)  # capture off: the input (B-2514)
        authz.insert_decisions(  # the refusal is a recorded policy decision (FR-GOV-005)
            self.session,
            [
                {
                    "id": uuid7(),
                    "tenant_id": self.sc.tenant_id,
                    "at": utcnow(),
                    "principal_id": row["initiated_by_principal_id"],
                    "principal_type": row["principal_type"],
                    "action": "run:start",
                    "resource_type": "run",
                    "resource_id": str(row["id"]),
                    "decision": "deny",
                    "reason_codes": ["budget_denied"],
                    "matched_binding_ids": [],
                    "policy_ids": [exc.budget_id] if exc.budget_id else [],
                    "run_id": row["id"],
                    "trace_id": row["trace_id"],
                }
            ],
        )
        self._audit("run.created", row, {"budget_denied": True}, outcome="denied")
        self._audit("run.failed", row, {"error_class": state.BUDGET_DENIED}, outcome="denied")
        self.session.commit()
        return self.run_out(row)

    # ================================================================== read
    def list_runs(
        self,
        *,
        status: str | None,
        kind: str | None,
        agent_id: UUID | None,
        workspace: str | None,
        project: str | None,
        initiated_by: UUID | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        cursor: str | None,
        outcome_unknown: bool = False,
    ) -> Page[s.RunOut]:
        ws_id = proj_id = None
        if workspace or project:
            ref = tenancy.scope_ref_from_slugs(self.sc, workspace, project)
            ws_id, proj_id = ref.workspace_id, ref.project_id
        rows = repo.list_runs(
            self.session,
            self.sc.tenant_id,
            after=decode_cursor(cursor),
            limit=limit + 1,
            status=status,
            kind=kind,
            agent_id=agent_id,
            workspace_id=ws_id,
            project_id=proj_id,
            initiated_by=initiated_by,
            since=since,
            until=until,
            outcome_unknown=outcome_unknown,
            scope=_scope_tuple(self.sc.pdp.visible_scopes("run:read")),
        )
        page, more = paginate(rows, limit)
        return Page(
            items=self._runs_out(page),
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, run_id: UUID) -> s.RunOut:
        return self.run_out(self._readable_run(run_id))

    def steps(self, run_id: UUID) -> list[s.StepOut]:
        run = self._readable_run(run_id)
        return [
            s.StepOut(
                id=st["id"],
                run_id=st["run_id"],
                seq=st["seq"],
                kind=st["kind"],
                name=st["name"],
                node_id=st["node_id"],
                status=st["status"],
                attempt=st["attempt"],
                started_at=st["started_at"],
                finished_at=st["finished_at"],
                model_id=st["model_id"],
                tool_version_id=st["tool_version_id"],
                tool_invocation_id=st["tool_invocation_id"],
                child_run_id=st["child_run_id"],
                idempotency_key=st["idempotency_key"],
                error_class=st["error_class"],
                error_message=st["error_message"],
                usage=dict(st["usage"] or {}),
                input_ref=st["input_ref"],
                output_ref=st["output_ref"],
            )
            for st in repo.list_steps(self.session, self.sc.tenant_id, run["id"])
        ]

    def events(self, run_id: UUID) -> list[dict[str, Any]]:  # Any: JSON documents
        run = self._readable_run(run_id)
        return [
            {
                "seq": e["seq"],
                "at": e["at"].isoformat(),
                "type": e["type"],
                "from": e["from_status"],
                "to": e["to_status"],
                "step_id": str(e["step_id"]) if e["step_id"] else None,
                "detail": e["details"] or {},
            }
            for e in repo.list_events(self.session, self.sc.tenant_id, run["id"])
        ]

    def result(self, run_id: UUID) -> s.ResultOut:
        run = self._readable_run(run_id)
        if run["status"] != state.COMPLETED:
            return s.ResultOut(status=run["status"], output=None)
        settings = tenancy.effective_settings(self.sc)
        mode = settings.get("capture_content")
        if mode is None:
            mode = "full" if self.runtime.settings.is_development else "metadata"
        if mode != "full":
            return s.ResultOut(status=run["status"], output=None, redacted=True)
        content = repo.get_artifact(self.session, self.sc.tenant_id, run["output_ref"]) or {}
        return s.ResultOut(status=run["status"], output=content.get("output"))

    # ================================================================== pipelines (P6)
    def pipeline_runs(self, pipeline_id: UUID, limit: int, cursor: str | None) -> Page[s.RunOut]:
        art = self.artifacts.artifact("pipelines", pipeline_id)  # pipeline:read, else 404
        rows = repo.list_runs(
            self.session,
            self.sc.tenant_id,
            after=decode_cursor(cursor),
            limit=limit + 1,
            status=None,
            kind="pipeline",
            agent_id=None,
            workspace_id=None,
            project_id=None,
            initiated_by=None,
            since=None,
            until=None,
            pipeline_id=art["id"],
            scope=_scope_tuple(self.sc.pdp.visible_scopes("run:read")),
        )
        page, more = paginate(rows, limit)
        return Page(
            items=self._runs_out(page),
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def _capture_full(self) -> bool:
        mode = tenancy.effective_settings(self.sc).get("capture_content")
        if mode is None:
            mode = "full" if self.runtime.settings.is_development else "metadata"
        return bool(mode == "full")

    def nodes(self, run_id: UUID) -> s.NodeListOut:
        run = self._readable_run(run_id)
        if run["kind"] != "pipeline":
            raise NotFound("run not found")
        tenant = self.sc.tenant_id
        payload = repo.get_artifact(self.session, tenant, run["input_ref"]) or {}
        spec = payload.get("spec_snapshot") or self.artifacts.version_spec(
            run["pipeline_version_id"]
        )
        by_node: dict[str, list[Mapping[str, Any]]] = {}
        for st in repo.list_steps(self.session, tenant, run["id"]):
            if st["kind"] == "pipeline_node":
                by_node.setdefault(st["node_id"], []).append(st)
        pending = repo.pending_approval_refs(self.session, tenant, run["id"])
        terminal = run["status"] in state.TERMINAL
        unknown_refs = repo.pending_unknown_refs(self.session, tenant, run["id"])
        child_unknown = repo.unknown_outcome_runs(  # one indexed probe when nothing is open
            self.session,
            tenant,
            [st["child_run_id"] for sts in by_node.values() for st in sts if st["child_run_id"]],
        )
        full = self._capture_full()
        out: list[s.NodeOut] = []
        for node in (spec or {}).get("nodes") or []:
            nid = str(node["id"])
            rows = by_node.get(nid, [])
            last = rows[-1] if rows else None
            status = self._node_status(last, f"node:{nid}" in pending, terminal)
            unknown = f"node:{nid}" in unknown_refs or any(
                r["child_run_id"] in child_unknown for r in rows if r["child_run_id"]
            )
            error_class = last["error_class"] if last is not None else None
            if unknown and terminal and run["status"] != state.COMPLETED:
                # not a plain failure / cancel: say what it is
                status, error_class = "failed", state.INDETERMINATE
            preview: Any = None
            ref: str | None = None
            if last is not None and last["status"] == "succeeded" and last["output_ref"]:
                ref = str(last["output_ref"])
                preview = self._preview(last["output_ref"], full)
            out.append(
                s.NodeOut(
                    node_id=nid,
                    type=str(node["type"]),
                    status=status,
                    attempts=max((int(r["attempt"]) for r in rows), default=0),
                    started_at=rows[0]["started_at"] if rows else None,
                    finished_at=last["finished_at"] if last is not None else None,
                    output_preview=preview,
                    output_ref=ref,
                    error_class=error_class,
                    outcome_unknown=unknown,
                )
            )
        return s.NodeListOut(items=out)

    @staticmethod
    def _node_status(last: Mapping[str, Any] | None, approval_pending: bool, terminal: bool) -> str:
        if last is None:
            return "cancelled" if terminal else "pending"
        status = str(last["status"])
        if status in ("succeeded", "failed", "skipped", "cancelled"):
            return status
        if status == "indeterminate":
            return "failed" if terminal else "waiting_for_approval"
        # running
        if terminal:
            return "cancelled"
        return "waiting_for_approval" if approval_pending else "running"

    def _preview(self, artifact_id: UUID, full: bool) -> Any:  # Any: redacted JSON
        if not full:
            return {"redacted": True}
        content = repo.get_artifact(self.session, self.sc.tenant_id, artifact_id) or {}
        doc = scrub(content.get("output") if isinstance(content, dict) else content)
        raw = json.dumps(doc, default=str)
        if len(raw.encode()) <= PREVIEW_BYTES:
            return doc
        return {"truncated": True, "preview": raw[: PREVIEW_BYTES - 200]}

    def artifact(self, run_id: UUID, artifact_id: UUID) -> s.ArtifactOut:
        run = self._readable_run(run_id)
        row = repo.get_artifact_row(self.session, self.sc.tenant_id, artifact_id)
        if (
            row is None
            or row["run_id"] != run["id"]
            or row["kind"] not in ("output", "intermediate")
        ):
            raise NotFound("artifact not found")
        base = {
            "id": row["id"],
            "run_id": row["run_id"],
            "kind": row["kind"],
            "size_bytes": int(row["size_bytes"]),
            "created_at": row["created_at"],
        }
        if not self._capture_full():
            return s.ArtifactOut(**base, redacted=True)
        if int(row["size_bytes"]) > ARTIFACT_BYTES:
            return s.ArtifactOut(**base, truncated=True)
        return s.ArtifactOut(**base, content=scrub(row["content"]))

    # ================================================================== cancel
    def cancel(self, run_id: UUID) -> s.RunOut:
        run = self._own_run(run_id)
        if run is None:
            raise NotFound("run not found")
        is_initiator = run["initiated_by_principal_id"] == self.sc.principal.principal_id
        if not is_initiator:
            # a run the caller cannot read answers like an unknown id ("run not found"), not with
            # the PDP's generic "resource not found" (runtime isolation review 10 finding 1)
            self._readable_run(run_id)
            self.sc.pdp.require("run:cancel", self._ref(run))
        self._cancel_tree(run)
        fresh = self._own_run(run_id)
        assert fresh is not None
        return self.run_out(fresh)

    def _cancel_tree(self, run: Mapping[str, Any]) -> None:  # Any: run row
        if run["status"] in state.TERMINAL:
            return
        # 1. flag the run, 2. cancel descendants, 3. finalize this run if nobody executes it.
        self._flag_cancel(run)
        for child in repo.children_of(self.session, self.sc.tenant_id, run["id"]):
            self._cancel_tree(child)
        for _ in range(3):
            current = self._own_run(run["id"])
            assert current is not None
            if current["status"] in state.TERMINAL or current["status"] == state.RUNNING:
                break
            row = repo.transition(
                self.session,
                self.sc.tenant_id,
                run["id"],
                current["status"],
                state.CANCELLED,
                clear_lease=True,
                finished_at=utcnow(),
                error_class=state.CANCELLED_CLASS,
                error_message="cancelled",
            )
            if row is not None:
                settle_terminal(self.session, row, state.CANCELLED, self.runtime.settings)
                self._audit("run.cancelled", row, {"by": "api"})
                self.session.commit()
                repo.finish_run_jobs(self.session, self.sc.tenant_id, run["id"])
                self.session.commit()
                return
            self.session.rollback()

    def _flag_cancel(self, run: Mapping[str, Any]) -> None:  # Any: run row
        current = self._own_run(run["id"], lock=True)
        if current is None or current["status"] in state.TERMINAL:
            self.session.rollback()
            return
        if current["cancel_requested_at"] is None:
            repo.request_cancel(
                self.session, self.sc.tenant_id, current["id"], self.sc.principal.principal_id
            )
            repo.add_event(
                self.session,
                current,
                "cancel_requested",
                details={"by": str(self.sc.principal.principal_id)},
            )
            self._audit("run.cancel_requested", current, {})
        self.session.commit()


class ApprovalService:
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.runtime = runtime

    @property
    def session(self) -> Session:
        return self.sc.session

    def _ref(self, row: Mapping[str, Any]) -> ResourceRef:  # Any: approval row
        return ResourceRef(self.sc.tenant_id, row["workspace_id"], row["project_id"])

    def _readable(self, approval_id: UUID) -> Mapping[str, Any]:  # Any: approval row
        tenant = self.sc.tenant_id
        row = repo.get_approval(self.session, tenant, approval_id)
        if row is not None and row["tenant_id"] != tenant:
            row = None
        if row is None or not self.sc.pdp.authorize("approval:read", self._ref(row)).allowed:
            raise NotFound("approval not found")
        return row

    def _out(self, row: Mapping[str, Any]) -> s.ApprovalOut:  # Any: approval row
        tenant = self.sc.tenant_id
        run = repo.get_run(self.session, tenant, row["run_id"])
        assert run is not None
        tool = (
            s.ToolRefOut(
                key=row["tool_key"],
                version=row["tool_version_no"],
                side_effect_class=row["side_effect_class"],
            )
            if row["tool_key"]
            else None
        )
        requested_by = s.PrincipalOut(
            type=run["principal_type"],
            id=row["requested_by"],
            display=repo.principal_display(
                self.session, tenant, run["principal_type"], row["requested_by"]
            ),
        )
        decision = None
        if row["decided_by"] is not None or row["decision"] is not None:
            decision = s.DecisionOut(
                decision=row["decision"],
                comment=row["decision_note"],
                decided_by=self._decider(row["decided_by"]) if row["decided_by"] else None,
                decided_at=row["decided_at"],
            )
        return s.ApprovalOut(
            id=row["id"],
            run_id=row["run_id"],
            step_id=repo.step_id_of(self.session, tenant, row["run_id"], row["step_seq"]),
            reason=row["reason"],
            tool=tool,
            arguments=row["arguments"],
            requested_by=requested_by,
            run_initiated_by=requested_by,
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            status=row["status"],
            decision=decision,
        )

    def _decider(self, principal_id: UUID) -> s.PrincipalOut:
        tenant = self.sc.tenant_id
        kind = repo.principal_type_of(self.session, tenant, principal_id)
        return s.PrincipalOut(
            type=kind,
            id=principal_id,
            display=repo.principal_display(self.session, tenant, kind, principal_id),
        )

    def list_approvals(
        self,
        status: str,
        run_id: UUID | None,
        limit: int,
        cursor: str | None,
        tree_run_id: UUID | None = None,
    ) -> Page[s.ApprovalOut]:
        statuses = STATUS_GROUPS.get(status)
        if statuses is None:
            raise ValidationFailed("status must be pending, decided or expired")
        rows = repo.list_approvals(
            self.session,
            self.sc.tenant_id,
            statuses=statuses,
            run_id=run_id,
            tree_run_id=tree_run_id,
            after=decode_cursor(cursor),
            limit=limit + 1,
            scope=_scope_tuple(self.sc.pdp.visible_scopes("approval:read")),
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, approval_id: UUID) -> s.ApprovalOut:
        return self._out(self._readable(approval_id))

    def decide(self, approval_id: UUID, body: s.DecideIn) -> s.ApprovalOut:
        probe = self._readable(approval_id)
        ref = self._ref(probe)
        self.sc.pdp.require("approval:decide", ref)
        if probe["required_permission"] != "approval:decide":
            # a governance approval rule named a further permission its approver must hold
            self.sc.pdp.require(str(probe["required_permission"]), ref)
        # lock order: run, then approval (the worker locks the run before it writes approvals)
        tenant = self.sc.tenant_id
        run = repo.lock_run(self.session, tenant, probe["run_id"])
        appr = repo.get_approval(self.session, tenant, approval_id, lock=True)
        if (
            run is None
            or appr is None
            or run["tenant_id"] != tenant
            or appr["tenant_id"] != tenant
            or appr["run_id"] != run["id"]
        ):
            self.session.rollback()
            raise NotFound("approval not found")
        if appr["status"] != "pending":
            raise AlreadyDecided(f"the approval is already {appr['status']}")
        indeterminate = appr["reason"] == "indeterminate_side_effect"
        # an unknown-outcome decision does not expire: it is decided by a human or stays open
        if not indeterminate and appr["expires_at"] <= utcnow():
            raise AlreadyDecided("the approval has expired")
        allowed = INDETERMINATE_DECISIONS if indeterminate else NORMAL_DECISIONS
        if body.decision not in allowed:
            raise ValidationFailed(f"decision must be one of {', '.join(allowed)}")
        if indeterminate and not (body.comment or "").strip():
            raise ValidationFailed("a comment is required to resolve an indeterminate side effect")
        if indeterminate and body.decision == "retry" and self._cannot_continue(run):
            raise ValidationFailed(
                "the run has ended or is past its deadline, so a retry is not possible: record "
                "whether the write happened (mark_succeeded) or declare it failed (fail)"
            )
        decider = self.sc.principal.principal_id
        own_run = decider == run["initiated_by_principal_id"] or decider in (
            repo.initiator_controllers(self.session, run)
        )
        if own_run and not tenancy.effective_settings(self.sc).get("allow_self_approval", False):
            self.session.rollback()
            raise SeparationOfDuties(
                "the person who started a run, or owns the service account / API key it ran as, "
                "cannot approve its actions"
            )
        if indeterminate and run["status"] == state.WAITING and run["deadline_at"] <= utcnow():
            # parked past its deadline and the reaper has not ended it yet: end it now (it keeps
            # this decision open), so the verdict below is recorded on the ended run
            ended = repo.transition(
                self.session,
                tenant,
                run["id"],
                state.WAITING,
                state.TIMED_OUT,
                clear_lease=True,
                finished_at=utcnow(),
                error_class=state.TIMEOUT,
                error_message="the run deadline passed",
            )
            if ended is not None:
                settle_terminal(self.session, ended, state.TIMED_OUT, self.runtime.settings)
                audit.record(  # the same event the worker writes for a deadline
                    self.session,
                    self.sc.ctx,
                    action="run.timed_out",
                    outcome="failure",
                    tenant_id=self.sc.tenant_id,
                    target_type="run",
                    target_id=run["id"],
                    target_label=f"run:{run['id']}",
                    workspace_id=run["workspace_id"],
                    project_id=run["project_id"],
                    details={
                        "run_id": str(run["id"]),
                        "kind": run["kind"],
                        "error_class": state.TIMEOUT,
                        "reason": "deadline_while_waiting",
                        "by": "approval_decision",
                    },
                )
                run = repo.get_run(self.session, tenant, run["id"]) or run
        rejecting = body.decision in ("reject", "fail")
        fail_children = False
        row = repo.decide_approval(
            self.session,
            tenant,
            approval_id,
            "rejected" if rejecting else "approved",
            body.decision,
            decider,
            (body.comment or "").strip() or None,
        )
        if row is None:
            self.session.rollback()
            raise AlreadyDecided("the approval was decided concurrently")
        repo.add_event(
            self.session,
            run,
            "approval_decided",
            step_id=repo.step_id_of(self.session, tenant, run["id"], appr["step_seq"]),
            details={"approval_id": str(approval_id), "decision": body.decision},
        )
        audit.record(
            self.session,
            self.sc.ctx,
            action="approval.decided",
            tenant_id=self.sc.tenant_id,
            target_type="approval",
            target_id=approval_id,
            target_label=f"approval:{approval_id}",
            workspace_id=run["workspace_id"],
            project_id=run["project_id"],
            details={
                "run_id": str(run["id"]),
                "decision": body.decision,
                "reason": appr["reason"],
                "tool": appr["tool_key"],
            },
        )
        if indeterminate and run["status"] in state.TERMINAL:
            self._record_verdict_on_ended_run(
                run, appr, succeeded=body.decision == "mark_succeeded"
            )
        if run["status"] == state.WAITING:
            if rejecting:
                code = state.INDETERMINATE if indeterminate else state.APPROVAL_REJECTED
                failed = repo.transition(
                    self.session,
                    tenant,
                    run["id"],
                    state.WAITING,
                    state.FAILED,
                    clear_lease=True,
                    finished_at=utcnow(),
                    error_class=code,
                    error_message="the approval was rejected"
                    if not indeterminate
                    else "an operator declared the side effect failed",
                )
                if failed is not None:
                    settle_terminal(self.session, failed, state.FAILED, self.runtime.settings)
                    fail_children = run["kind"] == "pipeline"
            elif repo.pending_approvals(self.session, tenant, run["id"]) == 0:
                back = repo.transition(
                    self.session,
                    tenant,
                    run["id"],
                    state.WAITING,
                    state.PENDING,
                    waiting_reason=None,
                )
                if back is not None:
                    repo.enqueue(self.session, tenant, run["id"])
        self.session.commit()
        if fail_children:  # the pipeline failed: its agent children must not keep running
            # the caller's client and the stored run's id (carry_forward RT-10/RT-11)
            cancel_open_children(
                self.session,
                tenant,
                run["id"],
                self.sc.principal.principal_id,
                self.runtime.settings,
            )
        return self._out(row)

    @staticmethod
    def _cannot_continue(run: Mapping[str, Any]) -> bool:  # Any: run row
        return run["status"] in state.TERMINAL or (
            run["status"] == state.WAITING and run["deadline_at"] <= utcnow()
        )

    def _record_verdict_on_ended_run(
        self,
        run: Mapping[str, Any],  # Any: run row
        appr: Mapping[str, Any],  # Any: approval row
        *,
        succeeded: bool,
    ) -> None:
        """The run ended while this write's outcome was unknown (C1): nothing resumes, so the
        operator's verdict goes straight onto the invocation / step and the run's message. The
        approval row itself was decided just above."""
        tenant = self.sc.tenant_id
        # applies this verdict to journal + step
        repo.settle_ended_run(self.session, tenant, run["id"])
        # none left: report the verdicts; some left: re-count them (and name the calls)
        message = repo.open_decisions_text(self.session, tenant, run["id"]) or repo.verdict_summary(
            self.session, tenant, run["id"]
        )
        repo.set_ended_run_message(self.session, tenant, run["id"], message)
        repo.add_event(
            self.session,
            run,
            "outcome_resolved",
            details={
                "approval_id": str(appr["id"]),
                "succeeded": succeeded,
                "previous_message": run["error_message"],
            },
        )
