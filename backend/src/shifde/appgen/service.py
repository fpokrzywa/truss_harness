"""App requests, their drafts and the `appgen.draft` job (SP5 design sections 4, 5.1, 5.2, 5.4 and
5.6).

A request belongs to one project of one organization. It names a Databricks connection the
requester can read, in the project's scope, with `access_mode` read, active, and discovered; the
tables it names come from that discovery, and the drafting model passes `models_admin`'s checks at
that project. Creating it queues one `appgen.draft` job in the same transaction. The job
(`run_draft_job`, run by `shifde-worker` through `shifde.worker_main`) re-derives the requester
from the database, checks the permission and the connection again, commits `drafting` before the
one model call (`ModelService.invoke_at`: budget hold, usage row and SHI switch stay in the
client; only names and types are sent), and records the outcome on the request: revision 1 and
`drafted`, or `failed` with a reason code. A job that finds its request already `drafting` (an
earlier attempt may have reached the provider) records `model_call_unknown` without calling again.

Request routes reach a request under the organization; an unknown id, another client's or another
project's request and one the caller cannot read get one answer (`app request not found`). Every
state change is a conditional UPDATE on the state read and writes one audit event.

Accepting a drafted revision (design 5.5) turns it into build, evaluation and packaging objects
of the request's project, each made through its owning service under the caller's scope. Those
services commit their own transactions, so accept is a resumable sequence. First a pre-check:
the caller must hold every permission the steps need on the project (a missing one is 403 with
nothing written); then the state, the revision's diagnostics, the runtime model and the artifact
keys are checked. Step 1 sets `accepting` (committed). Steps 2.1 to 2.6 create and publish the
prompt, one `connection.sql` tool per draft tool and the agent, then create the evaluation
dataset, the solution (or reuse the active one of that name) and the solution version with the
draft's screens. Each step locks the request row, re-reads `accept_progress`, is skipped when its
id is there, and records its id right after the owning service commits. Step 3 sets `accepted`.
A failed step records its error code as `status_reason`, writes `appgen.request.accept_failed`
(revision, step name and code, no draft content) and leaves the request `accepting` with its
objects in place; accepting the same revision again resumes.

One accept call at a time runs the steps: it holds the claim `accept_progress.claim` (a token and
a lease of `ACCEPT_CLAIM_SECONDS` in database time), taken by a conditional UPDATE (with
`accepting` on the first accept, before the key check on a resume) only when no claim is there
or its lease has passed, renewed by every recorded step and released when the accept ends. Every
write of the steps names the token, so a call whose lease another one took records nothing. A
resume that cannot take the claim is 409 `accept_in_progress` with nothing written; the API never
returns the claim. The prompt, tool and agent ids are minted and recorded before their create, so
a resume after a crash between a create and its next record adopts the artifact of the recorded
id (it must be of that kind, in the request's project, with the draft's key, else `key_taken`)
instead of finding the key taken; a key held by any other artifact is still `key_taken`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any  # Any: request and draft rows, JSON documents
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.appgen import draft as drafting
from shifde.appgen import repository, sqlcheck
from shifde.appgen import schemas as s
from shifde.appgen.render import render
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.build import schemas as build_schemas
from shifde.build.service import ArtifactService
from shifde.build.tools import named_sql
from shifde.connections.service import ConnectionService
from shifde.core import db, sqlrules
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.errors import AppError, NotFound, PublishPending
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.evaluation.schemas import DatasetCreate
from shifde.evaluation.service import EvaluationService
from shifde.models_admin import schemas as model_schemas
from shifde.models_admin.service import ModelService
from shifde.packaging import schemas as packaging_schemas
from shifde.packaging.service import (
    COMPILER_VERSION_APP,
    SolutionService,
    in_scope,
    scoped_release,
)
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

DRAFT_JOB_KIND = "appgen.draft"  # `shifde.jobs.kind` of a queued draft (design 5.2)
READ, WRITE = "appgen:request:read", "appgen:request:write"
RESOURCE_TYPE = "appgen_request"
REQUEST_NOT_FOUND = "app request not found"
DRAFT_MAX_TOKENS = 8000
DONE = frozenset({"drafted", "accepting", "accepted", "failed", "discarded"})
ACCEPT = "appgen:draft:accept"
# every permission the accept steps need on the project (design section 4): the pre-check
ACCEPT_PERMISSIONS = (
    ACCEPT,
    "prompt:create",
    "prompt:publish",
    "tool:create",
    "tool:publish",
    "agent:create",
    "agent:publish",
    "evaluation:dataset:write",
    "packaging:solution:write",
)
PUBLISH_NOTE = "accepted from an app request"
# the input schema of a draft tool parameter, by its draft type (design 5.5)
PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
    "string": {"type": "string", "maxLength": 1000},
}
TOOL_TIMEOUT_S = 60
AGENT_LIMITS = {"max_iterations": 8, "max_tool_calls": 8, "max_tokens": 200_000, "deadline_s": 60}
ACCEPT_CLAIM_SECONDS = 120  # the lease of an accept call's claim (database time), renewed per step
# the fields of a resolved tool the preview leaves out
PREVIEW_HIDDEN = {"version_id", "spec_hash", "input_schema"}
CLAIM = "claim"  # the `accept_progress` entry of the claim, never returned by the API


class ReleaseSourceChanged(AppError):
    status, code, title = 409, "release_source_changed", "Conflict"


SOURCE_CHANGED = "a version the release pins has changed or cannot be read; seal the release again"


class ReleaseHasNoApp(AppError):
    status, code, title = 409, "release_has_no_app", "Conflict"


class ConnectionNotDatabricks(AppError):
    status, code, title = 422, "connection_not_databricks", "Unprocessable Entity"


class ConnectionNotReadOnly(AppError):
    status, code, title = 422, "connection_not_read_only", "Unprocessable Entity"


class ConnectionNotActive(AppError):
    status, code, title = 422, "connection_not_active", "Unprocessable Entity"


class ConnectionNotDiscovered(AppError):
    status, code, title = 422, "connection_not_discovered", "Unprocessable Entity"


class TableNotDiscovered(AppError):
    status, code, title = 422, "table_not_discovered", "Unprocessable Entity"


class DraftModelUnavailable(AppError):
    status, code, title = 422, "model_unavailable", "Unprocessable Entity"


class DraftModelNotAllowed(AppError):
    status, code, title = 422, "model_not_allowed", "Unprocessable Entity"


class StaleRevision(AppError):
    status, code, title = 409, "stale_revision", "Conflict"


class RequestNotEditable(AppError):
    status, code, title = 409, "request_not_editable", "Conflict"


class RequestNotDiscardable(AppError):
    status, code, title = 409, "request_not_discardable", "Conflict"


class RequestNotFailed(AppError):
    status, code, title = 409, "request_not_failed", "Conflict"


class AlreadyAccepted(AppError):
    status, code, title = 409, "already_accepted", "Conflict"


class RequestNotAcceptable(AppError):
    status, code, title = 409, "request_not_acceptable", "Conflict"


class DraftInvalid(AppError):
    status, code, title = 422, "draft_invalid", "Unprocessable Entity"


class AcceptInProgress(AppError):
    status, code, title = 409, "accept_in_progress", "Conflict"


MODEL_REFUSALS: dict[str, AppError] = {
    "model_not_found": NotFound("model not found"),  # the answer an unknown model id gets
    "model_unavailable": DraftModelUnavailable("the drafting model cannot be used"),
    "model_not_allowed": DraftModelNotAllowed("the drafting model is not allowed here"),
}
# accept's refusals of the runtime model: the codes and statuses of `MODEL_REFUSALS`
RUNTIME_MODEL_REFUSALS: dict[str, AppError] = {
    "model_not_found": NotFound("model not found"),
    "model_unavailable": DraftModelUnavailable("the runtime model cannot be used"),
    "model_not_allowed": DraftModelNotAllowed("the runtime model is not allowed here"),
}
# the top-level message of a key check, by the error codes it found
KEY_MESSAGES = {
    frozenset({"invalid_key"}): "a name of the draft cannot be an artifact key",
    frozenset({"key_taken"}): "a name of the draft is already used in the project",
    frozenset({"invalid_key", "key_taken"}): "a name of the draft cannot be used in the project",
}


def _draft_out(r: Mapping[str, Any]) -> s.DraftOut:
    return s.DraftOut(
        revision_no=r["revision_no"],
        source=r["source"],
        draft=r["draft"],
        draft_hash=r["draft_hash"],
        validation=[s.Diagnostic.model_validate(d) for d in r["validation"]],
        created_by=r["created_by"],
        created_at=r["created_at"],
    )


def _summary_fields(r: Mapping[str, Any]) -> dict[str, Any]:  # Any: field values
    return {
        "id": r["id"],
        "workspace_id": r["workspace_id"],
        "project_id": r["project_id"],
        "connection_id": r["connection_id"],
        "status": r["status"],
        "status_reason": r["status_reason"],
        "latest_revision_no": r["latest_revision_no"],
        "solution_version_id": r["solution_version_id"],
        "requested_by": r["requested_by"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def sql_template(sql: str) -> str:
    """A draft tool's SQL in `build`'s stored form: each `:name` parameter `sqlrules.tokenize`
    finds (never one inside quoted text) as `%(name)s`, and every other `%` doubled, so
    `build.tools.named_sql` gives the draft SQL back."""
    parts: list[str] = []
    at = 0
    for token in sqlrules.tokenize(sql):
        if token.kind == "param":
            parts += [sql[at : token.start].replace("%", "%%"), f"%({token.value})s"]
            at = token.end
    parts.append(sql[at:].replace("%", "%%"))
    return "".join(parts)


def tool_spec(tool: s.DraftTool, connection_id: UUID) -> dict[str, Any]:  # Any: JSON spec
    """The `connection.sql` tool spec of a draft tool over the request's connection."""
    return {
        "executor": "connection.sql",
        "binding": {
            "connection_id": str(connection_id),
            "sql_template": sql_template(tool.sql),
            "max_rows": tool.max_rows,
        },
        "input_schema": {
            "type": "object",
            "properties": {p.name: dict(PARAMETER_SCHEMAS[p.type]) for p in tool.parameters},
            "required": [p.name for p in tool.parameters if p.required],
            "additionalProperties": False,
        },
        "output_schema": {"type": "object"},
        "side_effect_class": "read",
        "timeout_s": TOOL_TIMEOUT_S,
        "audit_level": "metadata",
    }


def _recorded(progress: Mapping[str, Any], key: str, item: str | None) -> str | None:
    """The id recorded under `key` (under `key.item` for a per-tool map), or None."""
    value = progress.get(key)
    if item is not None:
        value = value.get(item) if isinstance(value, dict) else None
    return str(value) if value is not None else None


@dataclass
class _Claim:
    """An accept call's claim token and the step it is running (named in `accept_failed`)."""

    token: UUID
    step: str = "prompt"


def _ref(tenant_id: UUID, r: Mapping[str, Any]) -> ResourceRef:
    return ResourceRef(tenant_id, r["workspace_id"], r["project_id"], RESOURCE_TYPE, r["id"])


def _record(
    session: Session,
    ctx: RequestContext,
    tenant_id: UUID,
    action: str,
    r: Mapping[str, Any],
    details: Mapping[str, object],
) -> None:
    audit.record(
        session,
        ctx,
        action=action,
        tenant_id=tenant_id,
        target_type=RESOURCE_TYPE,
        target_id=r["id"],
        workspace_id=r["workspace_id"],
        project_id=r["project_id"],
        details=dict(details),
    )


class AppGenService:
    def __init__(
        self, scope: TenantScope, runtime: IntegrationRuntime, evaluations: EvaluationService
    ) -> None:
        self.sc = scope
        self.runtime = runtime
        self.conns = ConnectionService(scope, runtime)
        self.models = ModelService(scope, runtime)
        self.artifacts = ArtifactService(scope, runtime)
        self.evaluations = evaluations
        self.solutions = SolutionService(scope, runtime)

    @property
    def _session(self) -> Session:
        return self.sc.session

    def _request(self, request_id: UUID, action: str) -> Mapping[str, Any]:
        """The request the caller may `action` on; anything else is `app request not found`."""
        row = repository.get_request(self._session, request_id, tenant_id=self.sc.tenant_id)
        if row is None:
            raise NotFound(REQUEST_NOT_FOUND)
        try:
            self.sc.pdp.require(action, _ref(self.sc.tenant_id, row), hide_unless=READ)
        except NotFound as exc:
            raise NotFound(REQUEST_NOT_FOUND) from exc
        return row

    def _out(self, request_id: UUID) -> s.AppRequestOut:
        row = repository.get_request(self._session, request_id, tenant_id=self.sc.tenant_id)
        if row is None:
            raise NotFound(REQUEST_NOT_FOUND)
        drafts = repository.list_drafts(self._session, request_id, tenant_id=self.sc.tenant_id)
        return s.AppRequestOut(
            **_summary_fields(row),
            tables=row["tables"],
            brief=row["brief"],
            model_id=row["model_id"],
            discovery_id=row["discovery_id"],
            accepted_revision_no=row["accepted_revision_no"],
            accept_progress={k: v for k, v in row["accept_progress"].items() if k != CLAIM},
            revisions=[_draft_out(d) for d in drafts],
        )

    def _audit(self, action: str, r: Mapping[str, Any], details: Mapping[str, object]) -> None:
        _record(self._session, self.sc.ctx, self.sc.tenant_id, action, r, details)

    def _requester_fields(self) -> dict[str, str | None]:
        """Who queues a job, as its payload carries it (re-derived when the job runs)."""
        p = self.sc.principal
        return {
            "principal_id": str(p.principal_id),
            "principal_type": p.principal_type,
            "api_key_id": str(p.api_key_id) if p.api_key_id else None,
        }

    def _enqueue(self, request_id: UUID, job_id: UUID) -> None:
        repository.enqueue_job(
            self._session,
            tenant_id=self.sc.tenant_id,
            kind=DRAFT_JOB_KIND,
            job_id=job_id,
            dedupe_key=f"{DRAFT_JOB_KIND}:{request_id}:{job_id}",
            payload={"request_id": str(request_id), **self._requester_fields()},
        )

    # ================================================================== create (design 5.1)
    def create(self, ws: str, proj: str, body: s.AppRequestCreate) -> s.AppRequestOut:
        project = tenancy.scope_ref_from_slugs(self.sc, ws, proj)
        self.sc.pdp.require(WRITE, project)
        assert project.workspace_id is not None and project.project_id is not None
        placement = self.conns.placement(body.connection_id)
        if placement is None or not in_scope(
            {"workspace_id": project.workspace_id, "project_id": project.project_id},
            placement.workspace_id,
            placement.project_id,
        ):
            raise NotFound("connection not found")
        conn = self.conns.get_row(body.connection_id)
        if conn["adapter_key"] != "databricks":
            raise ConnectionNotDatabricks("the connection is not a Databricks connection")
        if conn["access_mode"] != "read":
            raise ConnectionNotReadOnly("the connection is not read-only")
        if conn["status"] != "active":
            raise ConnectionNotActive("the connection is not active")
        discovery = self.conns.discovery_of(body.connection_id)
        if discovery is None or discovery.kind != "schema":
            raise ConnectionNotDiscovered("the connection's schema has not been discovered")
        known = {drafting.table_key(t) for t in discovery.payload.get("tables") or []}
        tables = [t.lower() for t in body.tables]
        missing = [(i, t) for i, t in enumerate(tables) if t not in known]
        if missing:
            raise TableNotDiscovered(
                "a table is not in the connection's latest discovery",
                errors=[
                    {
                        "loc": ["body", "tables", body.tables[i]],
                        "msg": f"{body.tables[i]} is not in the connection's latest discovery",
                        "type": TableNotDiscovered.code,
                    }
                    for i, _ in missing
                ],
            )
        problem = self.models.problems_at(project, [body.model_id]).get(body.model_id)
        if problem is not None:
            raise MODEL_REFUSALS[problem]
        request_id, job_id = uuid7(), uuid7()
        repository.insert_request(
            self._session,
            {
                "tenant_id": self.sc.tenant_id,
                "id": request_id,
                "workspace_id": project.workspace_id,
                "project_id": project.project_id,
                "connection_id": body.connection_id,
                "discovery_id": discovery.id,
                "tables": tables,
                "brief": body.brief,
                "model_id": body.model_id,
                "job_id": job_id,
                "requested_by": self.sc.principal.principal_id,
            },
        )
        self._enqueue(request_id, job_id)
        row = repository.get_request(self._session, request_id, tenant_id=self.sc.tenant_id)
        assert row is not None
        self._audit(
            "appgen.request.created",
            row,
            {"connection_id": str(body.connection_id), "tables": tables, "job_id": str(job_id)},
        )
        self._session.commit()
        return self._out(request_id)

    # ================================================================== read
    def list(
        self, ws: str, proj: str, *, status: str | None, limit: int, cursor: str | None
    ) -> Page[s.AppRequestSummary]:
        project = tenancy.scope_ref_from_slugs(self.sc, ws, proj)
        self.sc.pdp.require(READ, project)
        assert project.project_id is not None
        rows = repository.list_requests(
            self._session,
            tenant_id=self.sc.tenant_id,
            project_id=project.project_id,
            status=status,
            before=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[s.AppRequestSummary(**_summary_fields(r)) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get(self, request_id: UUID) -> s.AppRequestOut:
        self._request(request_id, READ)
        return self._out(request_id)

    def preview(self, release_id: UUID) -> s.AppPreviewOut:
        """The app a `/2` release renders to (design section 7): file metadata, screens and tools,
        gathered and rendered as plan time does under the caller's scope; nothing is written."""
        release = scoped_release(self.sc, release_id)
        manifest = release["manifest"]
        if manifest.get("compiler_version") != COMPILER_VERSION_APP:
            raise ReleaseHasNoApp("the release has no generated app")
        resolved = resolved_for_release(self.sc, self.runtime, manifest)
        files = render(manifest, release["digest"], resolved)
        return s.AppPreviewOut(
            release_id=release["id"],
            release_digest=release["digest"],
            generator_version=manifest["app"]["generator_version"],
            files=[
                s.AppPreviewFile(path=p, size=len(b), sha256=hashlib.sha256(b).hexdigest())
                for p, b in sorted(files.items())
            ],
            screens=resolved.screens,
            tools=[
                s.AppPreviewTool.model_validate(t.model_dump(exclude=PREVIEW_HIDDEN))
                for t in resolved.tools
            ],
        )

    # ================================================================== revise, discard, retry
    def revise(self, request_id: UUID, body: s.DraftEdit) -> s.DraftOut:
        # permission on an unlocked read (no lock wait for a caller without access), then the
        # row locked and read again: a second edit of the same base waits here, then is stale
        self._request(request_id, WRITE)
        row = repository.get_request(
            self._session, request_id, tenant_id=self.sc.tenant_id, lock=True
        )
        if row is None:
            raise NotFound(REQUEST_NOT_FOUND)
        if row["status"] != s.RequestStatus.DRAFTED:
            raise RequestNotEditable("the request is not drafted")
        if body.base_revision_no != row["latest_revision_no"]:
            raise StaleRevision("the draft has a newer revision; reload and retry")
        revision_no = body.base_revision_no + 1
        diagnostics = sqlcheck.validate(body.draft, row["tables"])
        repository.insert_draft(
            self._session,
            {
                "tenant_id": self.sc.tenant_id,
                "id": uuid7(),
                "request_id": request_id,
                "revision_no": revision_no,
                "source": "edited",
                "draft": body.draft.model_dump(mode="json"),
                "draft_hash": sqlcheck.draft_hash(body.draft),
                "validation": [d.model_dump() for d in diagnostics],
                "created_by": self.sc.principal.principal_id,
            },
        )
        # the revision is added only while `drafted` (a concurrent discard or second edit loses)
        if not repository.change_status(
            self._session,
            request_id,
            tenant_id=self.sc.tenant_id,
            expected=[s.RequestStatus.DRAFTED],
            status=s.RequestStatus.DRAFTED,
        ):
            self._session.rollback()
            raise RequestNotEditable("the request is not drafted")
        self._audit("appgen.draft.revised", row, {"revision_no": revision_no})
        self._session.commit()
        out = next(
            d
            for d in repository.list_drafts(self._session, request_id, tenant_id=self.sc.tenant_id)
            if d["revision_no"] == revision_no
        )
        return _draft_out(out)

    def discard(self, request_id: UUID) -> s.AppRequestOut:
        row = self._request(request_id, WRITE)
        expected = [s.RequestStatus.DRAFTED, s.RequestStatus.FAILED]
        if row["status"] not in expected or not repository.change_status(
            self._session,
            request_id,
            tenant_id=self.sc.tenant_id,
            expected=expected,
            status=s.RequestStatus.DISCARDED,
        ):
            self._session.rollback()
            raise RequestNotDiscardable("only a drafted or failed request can be discarded")
        self._audit("appgen.request.discarded", row, {"from": row["status"]})
        self._session.commit()
        return self._out(request_id)

    def retry(self, request_id: UUID) -> s.AppRequestOut:
        row = self._request(request_id, WRITE)
        job_id = uuid7()
        if row["status"] != s.RequestStatus.FAILED or not repository.change_status(
            self._session,
            request_id,
            tenant_id=self.sc.tenant_id,
            expected=[s.RequestStatus.FAILED],
            status=s.RequestStatus.QUEUED,
            job_id=job_id,
        ):
            self._session.rollback()
            raise RequestNotFailed("only a failed request can be retried")
        self._enqueue(request_id, job_id)
        self._audit(
            "appgen.request.retried",
            row,
            {"previous_reason": row["status_reason"], "job_id": str(job_id)},
        )
        self._session.commit()
        return self._out(request_id)

    # ================================================================== accept (design 5.5)
    def accept(self, request_id: UUID, body: s.AcceptIn) -> s.AppRequestOut:
        row = self._request(request_id, ACCEPT)
        project = ResourceRef(self.sc.tenant_id, row["workspace_id"], row["project_id"])
        for action in ACCEPT_PERMISSIONS:  # the pre-check: nothing is written before it passes
            self.sc.pdp.require(action, project)
        status = row["status"]
        if status == s.RequestStatus.ACCEPTED:
            raise AlreadyAccepted("the request is already accepted")
        if status == s.RequestStatus.ACCEPTING:
            if row["accepted_revision_no"] != body.revision_no:
                raise RequestNotAcceptable("the request is being accepted with another revision")
        elif status != s.RequestStatus.DRAFTED:
            raise RequestNotAcceptable("only a drafted request can be accepted")
        elif body.revision_no != row["latest_revision_no"]:
            raise StaleRevision("the draft has a newer revision; reload and retry")
        revision = next(
            d
            for d in repository.list_drafts(self._session, request_id, tenant_id=self.sc.tenant_id)
            if d["revision_no"] == body.revision_no
        )
        errors = [d for d in revision["validation"] if d["severity"] == "error"]
        if errors:
            raise DraftInvalid(
                "the revision has errors; correct them in a new revision",
                errors=[
                    {
                        "loc": ["draft", *d["path"].split(".")],
                        "msg": d["message"],
                        "type": d["code"],
                    }
                    for d in errors
                ],
            )
        problem = self.models.problems_at(project, [body.model_id]).get(body.model_id)
        if problem is not None:
            raise RUNTIME_MODEL_REFUSALS[problem]
        draft = s.Draft.model_validate(revision["draft"])
        claim = _Claim(uuid7())
        if status == s.RequestStatus.ACCEPTING:
            # the claim first, in the key check's transaction: a concurrent resume waits for the
            # row, then finds the claim taken; the keys are checked against what was recorded
            if not repository.claim_accepting(
                self._session,
                request_id,
                tenant_id=self.sc.tenant_id,
                revision_no=body.revision_no,
                token=claim.token,
                lease_seconds=ACCEPT_CLAIM_SECONDS,
            ):
                self._session.rollback()
                raise AcceptInProgress("the request is being accepted by another call")
            claimed = repository.get_request(self._session, request_id, tenant_id=self.sc.tenant_id)
            assert claimed is not None
            row = claimed
        try:
            self._check_keys(row, draft)
        except DraftInvalid:
            self._session.rollback()
            raise
        if status == s.RequestStatus.DRAFTED:
            if not repository.start_accepting(
                self._session,
                request_id,
                tenant_id=self.sc.tenant_id,
                revision_no=body.revision_no,
                accepted_by=self.sc.principal.principal_id,
                token=claim.token,
                lease_seconds=ACCEPT_CLAIM_SECONDS,
            ):
                self._session.rollback()
                raise RequestNotAcceptable("only a drafted request can be accepted")
            self._audit(
                "appgen.request.accepting",
                row,
                {"revision_no": body.revision_no, "solution_name": body.solution_name},
            )
        self._session.commit()
        try:
            version_id = self._accept_steps(row, draft, body, claim)
            claim.step = "finish"
            if not repository.finish_accepting(
                self._session,
                request_id,
                tenant_id=self.sc.tenant_id,
                solution_version_id=UUID(version_id),
                token=claim.token,
            ):
                self._session.rollback()
                raise RequestNotAcceptable("the request is no longer being accepted")
        except AppError as exc:
            self._session.rollback()
            # a call that lost its claim records nothing
            if repository.accept_failed(
                self._session,
                request_id,
                tenant_id=self.sc.tenant_id,
                reason=exc.code,
                token=claim.token,
            ):
                self._audit(
                    "appgen.request.accept_failed",
                    row,
                    {
                        "revision_no": body.revision_no,
                        "step": claim.step,
                        "status_reason": exc.code,
                    },
                )
            self._session.commit()
            raise
        self._audit(
            "appgen.request.accepted",
            row,
            {"revision_no": body.revision_no, "solution_version_id": version_id},
        )
        self._session.commit()
        return self._out(request_id)

    def _check_keys(self, row: Mapping[str, Any], draft: s.Draft) -> None:
        """The draft's artifact keys are usable and free in the project (`draft_invalid`, naming
        the field, otherwise); a key is free when no artifact holds it or the artifact holding it
        has the id this request recorded for it."""
        progress = row["accept_progress"]
        name = draft.agent.name
        checks = [
            ("prompts", name, _recorded(progress, "prompt_artifact_id", None), "agent.name"),
            ("agents", name, _recorded(progress, "agent_artifact_id", None), "agent.name"),
        ] + [
            ("tools", t.name, _recorded(progress, "tool_artifact_ids", t.name), f"tools.{i}.name")
            for i, t in enumerate(draft.tools)
        ]
        errors: list[dict[str, Any]] = []  # Any: problem+json error fields
        for kind, key, recorded, path in checks:
            if not re.match(build_schemas.KEY_PATTERN, key):
                msg, code = f"{key!r} cannot be an artifact key", "invalid_key"
            elif (
                holder := self.artifacts.key_holder(
                    kind, row["workspace_id"], row["project_id"], key
                )
            ) is not None and str(holder) != recorded:
                msg, code = f"the project already has a {kind[:-1]} {key!r}", "key_taken"
            else:
                continue
            errors.append({"loc": ["draft", *path.split(".")], "msg": msg, "type": code})
        if errors:
            raise DraftInvalid(KEY_MESSAGES[frozenset(e["type"] for e in errors)], errors=errors)

    def _progress(self, request_id: UUID) -> dict[str, Any]:  # Any: JSON ids
        """The request's `accept_progress`, its row locked until this transaction ends (only the
        claim holder runs the steps; the lock orders its reads after any write it waits for)."""
        row = repository.get_request(
            self._session, request_id, tenant_id=self.sc.tenant_id, lock=True
        )
        if row is None or row["status"] != s.RequestStatus.ACCEPTING:
            raise RequestNotAcceptable("the request is no longer being accepted")
        return dict(row["accept_progress"])

    def _note(
        self, request_id: UUID, claim: _Claim, key: str, value: str, item: str | None = None
    ) -> None:
        """Record `value` under `key` (under `key.item` for a per-tool map), renewing the claim,
        and commit; a call that no longer holds the claim records nothing."""
        progress = self._progress(request_id)
        stored: Any = value  # Any: an id or a per-tool map of ids
        if item is not None:
            stored = {**(progress.get(key) or {}), item: value}
        if not repository.record_progress(
            self._session,
            request_id,
            tenant_id=self.sc.tenant_id,
            key=key,
            value=stored,
            token=claim.token,
            lease_seconds=ACCEPT_CLAIM_SECONDS,
        ):
            self._session.rollback()
            raise RequestNotAcceptable("the request is no longer being accepted")
        self._session.commit()

    def _published(self, kind: str, artifact_id: UUID) -> str:
        """The artifact's draft published now, or its latest published version when it has no
        draft left; a publish waiting for approval is `publish_pending`."""
        art = self.artifacts.get(kind, artifact_id)
        if art.has_draft or art.latest_published_version is None:
            out = self.artifacts.publish(
                kind, artifact_id, build_schemas.PublishIn(change_note=PUBLISH_NOTE)
            )
            if isinstance(out, build_schemas.PublishPendingOut):
                raise PublishPending("the publish waits for approval; accept again once approved")
            return str(out.id)
        return str(art.latest_published_version.id)

    def _adopted(
        self, row: Mapping[str, Any], kind: str, artifact_id: UUID, key: str, path: str
    ) -> bool:
        """Whether the artifact of the id this request minted exists (its create committed before
        its version was recorded); one that is not the draft's artifact of the request's project
        is `key_taken`."""
        try:
            found = self.artifacts.artifact_row(kind, artifact_id)
        except NotFound:
            return False
        placed = (found["workspace_id"], found["project_id"], found["key"])
        if placed != (row["workspace_id"], row["project_id"], key):
            raise DraftInvalid(
                KEY_MESSAGES[frozenset({"key_taken"})],
                errors=[
                    {
                        "loc": ["draft", *path.split(".")],
                        "msg": f"the project already has a {kind[:-1]} {key!r}",
                        "type": "key_taken",
                    }
                ],
            )
        return True

    def _artifact_step(
        self,
        row: Mapping[str, Any],
        claim: _Claim,
        step: str,
        kind: str,
        body: build_schemas.ArtifactCreate,
        keys: tuple[str, str],
        path: str,
        item: str | None = None,
    ) -> str:
        """Steps 2.1 to 2.3: the published version of one artifact, unless `keys` (artifact id,
        version id) already record it. The artifact id is minted and recorded before the create;
        a recorded id whose artifact exists is adopted, one with no artifact is created."""
        claim.step = step
        artifact_key, version_key = keys
        progress = self._progress(row["id"])
        version = _recorded(progress, version_key, item)
        if version is not None:
            return version
        artifact = _recorded(progress, artifact_key, item)
        if artifact is None:
            artifact = str(uuid7())
            self._note(row["id"], claim, artifact_key, artifact, item)
            self.artifacts.create(kind, body, artifact_id=UUID(artifact))
        elif not self._adopted(row, kind, UUID(artifact), body.key, path):
            self.artifacts.create(kind, body, artifact_id=UUID(artifact))
        version = self._published(kind, UUID(artifact))
        self._note(row["id"], claim, version_key, version, item)
        return version

    def _step(
        self, row: Mapping[str, Any], claim: _Claim, key: str, make: Callable[[], UUID]
    ) -> str:
        """Steps 2.4 to 2.6: the id recorded under `key`, or `make()`'s, recorded now."""
        claim.step = key.removesuffix("_id")
        recorded = _recorded(self._progress(row["id"]), key, None)
        if recorded is not None:
            return recorded
        made = str(make())
        self._note(row["id"], claim, key, made)
        return made

    def _accept_steps(
        self, row: Mapping[str, Any], draft: s.Draft, body: s.AcceptIn, claim: _Claim
    ) -> str:
        """Steps 2.1 to 2.6; returns the solution version id."""
        workspaces, projects = tenancy.scope_slug_map(
            self.sc, [row["workspace_id"]], [row["project_id"]]
        )
        ws, proj = workspaces[row["workspace_id"]], projects[row["project_id"]]
        scope = build_schemas.ScopeRef(type="project", id=row["project_id"])
        name = draft.agent.name

        def artifact(
            key: str, spec: dict[str, Any], description: str | None = None
        ) -> build_schemas.ArtifactCreate:
            return build_schemas.ArtifactCreate(
                key=key, name=key, description=description, scope=scope, spec=spec
            )

        prompt = self._artifact_step(
            row,
            claim,
            "prompt",
            "prompts",
            artifact(name, {"sections": {"system": draft.agent.instructions}}),
            ("prompt_artifact_id", "prompt_version_id"),
            "agent.name",
        )
        tools = [
            self._artifact_step(
                row,
                claim,
                "tools",
                "tools",
                artifact(tool.name, tool_spec(tool, row["connection_id"]), tool.description),
                ("tool_artifact_ids", "tool_version_ids"),
                f"tools.{i}.name",
                tool.name,
            )
            for i, tool in enumerate(draft.tools)
        ]
        thresholds = body.thresholds
        agent = self._artifact_step(
            row,
            claim,
            "agent",
            "agents",
            artifact(
                name,
                {
                    "prompt_version_id": prompt,
                    "model": {"model_id": str(body.model_id)},
                    "tool_version_ids": tools,
                    # a normalized decimal string is a JSON number
                    "limits": {
                        **AGENT_LIMITS,
                        "max_cost": json.loads(thresholds.max_total_cost_usd),
                    },
                },
            ),
            ("agent_artifact_id", "agent_version_id"),
            "agent.name",
        )

        def dataset() -> UUID:
            cases = [c.model_dump(mode="json") for c in draft.evaluation_cases]
            return self.evaluations.create_dataset(
                ws, proj, DatasetCreate.model_validate({"name": body.solution_name, "cases": cases})
            ).id

        dataset_id = self._step(row, claim, "dataset_id", dataset)

        def solution() -> UUID:
            found = self.solutions.active_named(ws, proj, body.solution_name)
            if found is None:
                found = self.solutions.create(
                    ws,
                    proj,
                    packaging_schemas.SolutionCreate(
                        name=body.solution_name, target_kind=packaging_schemas.TARGET_KIND
                    ),
                )
            return found.id

        solution_id = self._step(row, claim, "solution_id", solution)

        def version() -> UUID:
            read = packaging_schemas.DataRead(
                connection_id=row["connection_id"], tables=row["tables"]
            )
            contract = packaging_schemas.EvaluationContract(
                dataset_id=UUID(dataset_id), **thresholds.model_dump()
            )
            return self.solutions.create_version(
                ws,
                proj,
                UUID(solution_id),
                packaging_schemas.SolutionVersionCreate(
                    entrypoint_kind="agent",
                    entrypoint_version_id=UUID(agent),
                    data_reads=[read],
                    evaluation=contract,
                    screens=draft.screens,
                ),
            ).id

        return self._step(row, claim, "solution_version_id", version)


# ====================================================================== Resolved (design 6.3)
def _parameter_type(prop: Any) -> str:  # Any: an input schema property
    """The draft type of an input schema property (the inverse of `PARAMETER_SCHEMAS`)."""
    kind = prop.get("type") if isinstance(prop, dict) else None
    if kind in ("integer", "number", "boolean"):
        return str(kind)
    if kind == "string" and prop.get("format") == "date":
        return "date"
    return "string"


def _version_uuid(value: Any) -> UUID | None:  # Any: a JSON value
    try:
        return UUID(str(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def resolved_for_release(
    scope: TenantScope, runtime: IntegrationRuntime, manifest: Mapping[str, Any]
) -> s.Resolved:  # Any: the release manifest
    """`Resolved` of a `/2` release (design 6.3): its pinned agent, prompt and tool versions read
    through `build`'s service reads under `scope` (the plan requester's organization and RLS
    context, never another), the release's single model binding's `model` as the serving endpoint
    and the manifest's screens. Every version the manifest pins (the entrypoint and each pin) must
    exist in this organization with the pinned `spec_hash`, and the agent may name only pinned
    versions; anything else is 409 `release_source_changed`, with nothing written."""
    artifacts = ArtifactService(scope, runtime)
    pinned: dict[UUID, str] = {}
    for item in [manifest.get("entrypoint") or {}, *(manifest.get("pins") or [])]:
        version_id = _version_uuid(item.get("version_id"))
        if version_id is None:
            raise ReleaseSourceChanged(SOURCE_CHANGED)
        pinned[version_id] = str(item.get("spec_hash"))
    kinds: dict[UUID, str] = {}
    for version_id, spec_hash in pinned.items():
        access = artifacts.version_access(version_id)
        if access is None or access.spec_hash != spec_hash:
            raise ReleaseSourceChanged(SOURCE_CHANGED)
        kinds[version_id] = access.kind

    def read(value: Any, kind: str) -> tuple[Mapping[str, Any], dict[str, Any]]:  # Any: JSON
        """(artifact row, spec) of the pinned version `value` of `kind`."""
        version_id = _version_uuid(value)
        if version_id is None or kinds.get(version_id) != kind:
            raise ReleaseSourceChanged(SOURCE_CHANGED)
        spec = artifacts.version_spec(version_id)
        access = artifacts.version_access(version_id)
        if spec is None or access is None:
            raise ReleaseSourceChanged(SOURCE_CHANGED)
        return artifacts.artifact_row(kind, access.artifact_id), spec

    agent_art, agent = read((manifest.get("entrypoint") or {}).get("version_id"), "agents")
    _, prompt = read(agent.get("prompt_version_id"), "prompts")
    tools = []
    for value in agent.get("tool_version_ids") or []:
        tool_art, tool = read(value, "tools")
        properties = tool["input_schema"].get("properties") or {}
        required = set(tool["input_schema"].get("required") or [])
        tools.append(
            {
                "version_id": value,
                "spec_hash": pinned[UUID(str(value))],
                "name": tool_art["key"],
                "description": tool_art["description"] or "",
                "sql": named_sql(tool["binding"]["sql_template"]),
                "parameters": [
                    {"name": n, "type": _parameter_type(properties[n]), "required": n in required}
                    for n in sorted(properties)
                ],
                "input_schema": tool["input_schema"],
                "max_rows": tool["binding"]["max_rows"],
            }
        )
    entry = UUID(str(manifest["entrypoint"]["version_id"]))
    model_bindings = manifest["bindings"]["model_bindings"]
    return s.Resolved.model_validate(
        {
            "agent": {
                "version_id": entry,
                "spec_hash": pinned[entry],
                "name": agent_art["key"],
                "instructions": str((prompt.get("sections") or {}).get("system") or ""),
                "limits": {key: agent["limits"][key] for key in AGENT_LIMITS},
            },
            "tools": sorted(tools, key=lambda t: t["name"]),
            "model": {"serving_endpoint": next(iter(model_bindings.values()))["model"]},
            "screens": manifest.get("screens") or [],
        }
    )


# ====================================================================== the job (design 5.2)
@dataclass(frozen=True)
class _Requester:
    """Who queued a draft (`authz.execution.RunRef`): re-derived when the job runs."""

    tenant_id: UUID
    initiated_by_principal_id: UUID
    principal_type: str
    api_key_id: UUID | None


def _job_scope(
    session: Session, tenant_id: UUID, request_id: str, payload: Mapping[str, Any]
) -> TenantScope:
    """The scope of the job's requester, re-derived from the database (`ExecutionDenied` when the
    requester or the client is no longer available)."""
    principal = authz.execution_principal(
        session,
        _Requester(
            tenant_id,
            UUID(payload["principal_id"]),
            payload["principal_type"],
            UUID(payload["api_key_id"]) if payload.get("api_key_id") else None,
        ),
    )
    header = tenancy.tenant_header(session, tenant_id)
    if header is None:
        raise authz.ExecutionDenied("the client is not available")
    return TenantScope(
        session=session,
        ctx=RequestContext(request_id=request_id, principal=principal, tenant_id=tenant_id),
        principal=principal,
        tenant_id=tenant_id,
        tenant_slug=header[0],
        tenant_name=header[1],
        tenant_status=header[2],
        pdp=authz.new_pdp(session, principal, tenant_id),
    )


def _fail(
    session: Session,
    ctx: RequestContext,
    tenant_id: UUID,
    row: Mapping[str, Any],
    expected: str,
    reason: str,
    detail: Mapping[str, object] | None = None,
) -> None:
    """Record `failed` with `reason` (when still `expected`) and its audit event; commits."""
    if repository.change_status(
        session,
        row["id"],
        tenant_id=tenant_id,
        expected=[expected],
        status=s.RequestStatus.FAILED,
        status_reason=reason,
    ):
        _record(
            session,
            ctx,
            tenant_id,
            "appgen.request.failed",
            row,
            {"reason": reason, **(detail or {})},
        )
    session.commit()


def _connection_usable(
    scope: TenantScope, runtime: IntegrationRuntime, connection_id: UUID
) -> bool:
    conns = ConnectionService(scope, runtime)
    if conns.placement(connection_id) is None:
        return False
    row = conns.get_row(connection_id)
    return bool(
        row["adapter_key"] == "databricks"
        and row["access_mode"] == "read"
        and row["status"] == "active"
    )


def run_draft_job(
    database: Database, runtime: IntegrationRuntime, tenant_id: UUID, job_id: UUID
) -> str | None:
    """Execute the `appgen.draft` job `job_id` of `tenant_id` (called by `shifde-worker`).

    Returns None once the outcome is recorded on the request (or there is nothing to do: the
    request is gone, already past drafting, or names a newer job), `not_found` when the job is
    not this client's draft job. The model is called at most once per job."""
    request_tag = f"appgen-draft-{job_id}"
    session = database.session()
    db.init_context(session, request_tag)
    try:
        db.set_tenant(session, tenant_id)
        job = repository.get_job(session, job_id, tenant_id=tenant_id, kind=DRAFT_JOB_KIND)
        if job is None:
            return NotFound.code
        payload = job["payload"]
        row = repository.get_request(session, UUID(payload["request_id"]), tenant_id=tenant_id)
        if row is None or row["status"] in DONE or row["job_id"] != job_id:
            return None
        system_ctx = RequestContext(request_id=request_tag, tenant_id=tenant_id)
        if row["status"] == s.RequestStatus.DRAFTING:
            # an earlier attempt may have reached the provider: never call again (design 5.6)
            _fail(session, system_ctx, tenant_id, row, row["status"], "model_call_unknown")
            return None
        try:
            scope = _job_scope(session, tenant_id, request_tag, payload)
        except authz.ExecutionDenied:
            session.rollback()
            _fail(session, system_ctx, tenant_id, row, row["status"], "access_revoked")
            return None
        ctx = scope.ctx
        ref = _ref(tenant_id, row)
        if not scope.pdp.authorize(WRITE, ref).allowed:
            _fail(session, ctx, tenant_id, row, row["status"], "access_revoked")
            return None
        discovery = repository.get_discovery(
            session, row["discovery_id"], tenant_id=tenant_id, connection_id=row["connection_id"]
        )
        if discovery is None or not _connection_usable(scope, runtime, row["connection_id"]):
            _fail(session, ctx, tenant_id, row, row["status"], "connection_unavailable")
            return None
        if not repository.change_status(
            session,
            row["id"],
            tenant_id=tenant_id,
            expected=[s.RequestStatus.QUEUED],
            status=s.RequestStatus.DRAFTING,
            for_job=job_id,
        ):
            session.rollback()
            return None
        _record(session, ctx, tenant_id, "appgen.request.drafting", row, {"job_id": str(job_id)})
        session.commit()  # `drafting` is committed before the model call (design 5.6)
        body = model_schemas.InvokeIn(
            messages=[
                model_schemas.ChatMessageIn(role="system", content=drafting.SYSTEM_PROMPT),
                model_schemas.ChatMessageIn(
                    role="user",
                    content=drafting.user_message(
                        row["brief"], discovery["payload"], row["tables"]
                    ),
                ),
            ],
            max_tokens=DRAFT_MAX_TOKENS,
        )
        try:
            out = ModelService(scope, runtime, database).invoke_at(ref, row["model_id"], body)
        except AppError as exc:
            session.rollback()
            _fail(session, ctx, tenant_id, row, s.RequestStatus.DRAFTING, exc.code)
            return None
        parsed = drafting.parse_reply(out.output)
        if isinstance(parsed, list):
            _fail(
                session,
                ctx,
                tenant_id,
                row,
                s.RequestStatus.DRAFTING,
                "invalid_model_output",
                {"messages": parsed},
            )
            return None
        diagnostics = sqlcheck.validate(parsed, row["tables"])
        repository.insert_draft(
            session,
            {
                "tenant_id": tenant_id,
                "id": uuid7(),
                "request_id": row["id"],
                "revision_no": 1,
                "source": "model",
                "draft": parsed.model_dump(mode="json"),
                "draft_hash": sqlcheck.draft_hash(parsed),
                "validation": [d.model_dump() for d in diagnostics],
                "created_by": scope.principal.principal_id,
            },
        )
        if not repository.change_status(
            session,
            row["id"],
            tenant_id=tenant_id,
            expected=[s.RequestStatus.DRAFTING],
            status=s.RequestStatus.DRAFTED,
        ):
            session.rollback()
            return None
        _record(
            session,
            ctx,
            tenant_id,
            "appgen.request.drafted",
            row,
            {"revision_no": 1, "errors": sum(1 for d in diagnostics if d.severity == "error")},
        )
        session.commit()
        return None
    finally:
        session.close()
