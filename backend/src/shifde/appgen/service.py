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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any  # Any: request and draft rows, JSON documents
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.appgen import draft as drafting
from shifde.appgen import repository, sqlcheck
from shifde.appgen import schemas as s
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.connections.service import ConnectionService
from shifde.core import db
from shifde.core.context import RequestContext
from shifde.core.db import Database
from shifde.core.errors import AppError, NotFound
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.models_admin import schemas as model_schemas
from shifde.models_admin.service import ModelService
from shifde.packaging.service import in_scope
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

DRAFT_JOB_KIND = "appgen.draft"  # `shifde.jobs.kind` of a queued draft (design 5.2)
READ, WRITE = "appgen:request:read", "appgen:request:write"
RESOURCE_TYPE = "appgen_request"
REQUEST_NOT_FOUND = "app request not found"
DRAFT_MAX_TOKENS = 8000
DONE = frozenset({"drafted", "accepting", "accepted", "failed", "discarded"})


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


MODEL_REFUSALS: dict[str, AppError] = {
    "model_not_found": NotFound("model not found"),  # the answer an unknown model id gets
    "model_unavailable": DraftModelUnavailable("the drafting model cannot be used"),
    "model_not_allowed": DraftModelNotAllowed("the drafting model is not allowed here"),
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
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.runtime = runtime
        self.conns = ConnectionService(scope, runtime)
        self.models = ModelService(scope, runtime)

    @property
    def _session(self) -> Session:
        return self.sc.session

    def _request(self, request_id: UUID, action: str, *, lock: bool = False) -> Mapping[str, Any]:
        """The request the caller may `action` on; anything else is `app request not found`.
        `lock` holds its row until commit, so concurrent writers of it run one at a time."""
        row = repository.get_request(
            self._session, request_id, tenant_id=self.sc.tenant_id, lock=lock
        )
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
            accept_progress=row["accept_progress"],
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

    # ================================================================== revise, discard, retry
    def revise(self, request_id: UUID, body: s.DraftEdit) -> s.DraftOut:
        # locked: a second edit of the same base waits here, then sees the new revision
        row = self._request(request_id, WRITE, lock=True)
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
