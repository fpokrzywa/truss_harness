"""Evaluation datasets, evaluation runs, scoring and the deployment gate's check (sub-project 3
design section 6, settled calls 4, 7, 16 to 22; lead decisions B-2703, B-2706 to B-2708).

A dataset belongs to one project; its routes name the workspace and project in the path, and a
dataset that is not in that project, or that the caller cannot read, gets the answer an unknown
id gets (`dataset not found`). A dataset never changes: writing a name again creates its next
version. Its `digest` is the sha256 of the cases' canonical JSON (`build.canonical`; cases may
hold floats, which `packaging.canonical` refuses).

An evaluation run evaluates one release against the dataset its manifest names. The release
comes from `packaging`'s service with its solution's scope; an evaluation run's `ResourceRef`
has that scope, so an unknown, foreign or unreadable release is `release not found` and an
unknown, foreign or unreadable evaluation run is `evaluation run not found`. A request is
refused (and writes nothing) for a withdrawn release (`release_withdrawn`), a dataset that no
longer resolves in the release's client, workspace and project or whose digest differs from the
manifest's (`evaluation_dataset_unavailable`, one body for every case), and an organization that
does not capture run content in full (`evaluation_requires_full_capture`, B-2708: scoring reads
the case outputs).

Settled call 16's fallback: `runtime` sits below `evaluation` in the layers contract, so its
worker cannot dispatch an `evaluation.run` job without a change to runtime's claim and dispatch.
The request therefore starts the case runs itself, one ordinary SETH run per case, through
`RunService.create` as the requesting user, in the release's workspace and project, of the
release's entrypoint version (they run concurrently, B-2706). Each case run calls the release's
bound models (the client's own model of the binding's provider and served name, B-2707; a binding
that names none ends the evaluation `error` before any case run starts) within a cost cap of the
threshold divided by the number of cases, rounded down (B-2706). Scoring finishes the evaluation
when it is read (both read routes) and from `require_passing_evaluation`: under the row lock,
once every case run has ended, it writes the results once.

Every row is read and written for the caller's resolved client only (`TenantScope.tenant_id`);
`require_passing_evaluation` keys every statement on the session's client.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any  # Any: rows and JSON documents
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz.service import ResourceRef
from shifde.build.canonical import canonical_json, spec_hash
from shifde.build.service import ArtifactService
from shifde.core.context import RequestContext
from shifde.core.errors import AppError, Conflict, NotFound, ScopeArchived
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.evaluation import assertions, repository, scoring
from shifde.evaluation import schemas as s
from shifde.models_admin.service import ModelService
from shifde.packaging.service import release_record
from shifde.runtime import state
from shifde.runtime.governance import GovernanceHooks
from shifde.runtime.schemas import RunCreate
from shifde.runtime.service import RunService, run_outcome
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope

DATASET_READ, DATASET_WRITE = "evaluation:dataset:read", "evaluation:dataset:write"
RUN_READ, RUN_REQUEST = "evaluation:run:read", "evaluation:run:request"
DATASET_VERSION_TAKEN = "a concurrent request created this dataset version; try again"
OUTPUT_NOT_KEPT = "the case output was not kept"
COST_UNKNOWN = "the case cost is unknown"
MAX_REASON = 2000


class ReleaseWithdrawn(AppError):
    status, code, title = 409, "release_withdrawn", "Conflict"


class DatasetUnavailable(AppError):
    status, code, title = 409, "evaluation_dataset_unavailable", "Conflict"


class FullCaptureRequired(AppError):
    status, code, title = 409, "evaluation_requires_full_capture", "Conflict"


class EvaluationRequired(AppError):
    status, code, title = 409, "evaluation_required", "Conflict"


def dataset_in_project(
    session: Session, tenant_id: UUID, dataset_id: UUID, workspace_id: UUID, project_id: UUID
) -> Mapping[str, Any] | None:
    """The dataset `dataset_id` only if it is a dataset of `tenant_id` in that workspace and
    project; None otherwise (unknown, another client's or another project's alike)."""
    row = repository.get_dataset(session, dataset_id, tenant_id=tenant_id)
    if (
        row is None
        or row["tenant_id"] != tenant_id
        or row["workspace_id"] != workspace_id
        or row["project_id"] != project_id
    ):
        return None
    return row


def _uuid(value: Any) -> UUID | None:  # Any: a JSON value
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _run_out(row: Mapping[str, Any]) -> s.EvaluationRunOut:
    accuracy = row["accuracy"]
    return s.EvaluationRunOut(
        id=row["id"],
        release_id=row["release_id"],
        release_digest=row["release_digest"],
        dataset_id=row["dataset_id"],
        dataset_digest=row["dataset_digest"],
        thresholds=dict(row["thresholds"]),
        status=row["status"],
        status_reason=row["status_reason"],
        accuracy=scoring.decimal_text(Decimal(accuracy)) if accuracy is not None else None,
        p95_latency_ms=row["p95_latency_ms"],
        total_cost_usd=row["total_cost_usd"],
        results=[s.CaseResult(**r) for r in row["results"]] if row["results"] is not None else None,
        requested_by=row["requested_by"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_at=row["created_at"],
    )


def _audit_run(
    session: Session,
    ctx: RequestContext,
    action: str,
    release: Mapping[str, Any],
    run_id: UUID,
    details: dict[str, object],
    *,
    outcome: str = "success",
) -> None:
    audit.record(
        session,
        ctx,
        action=action,
        outcome=outcome,
        tenant_id=release["tenant_id"],
        target_type="evaluation_run",
        target_id=run_id,
        target_label=f"evaluation_run:{run_id}",
        workspace_id=release["workspace_id"],
        project_id=release["project_id"],
        details={"release_id": str(release["id"]), **details},
    )


def _finish(
    session: Session,
    ctx: RequestContext,
    release: Mapping[str, Any],
    run_id: UUID,
    values: dict[str, Any],
) -> None:
    if values.get("status_reason"):  # the column holds at most 2000 characters
        values["status_reason"] = values["status_reason"][:MAX_REASON]
    repository.finish_run(session, run_id, values, tenant_id=release["tenant_id"])
    _audit_run(
        session,
        ctx,
        "evaluation.run.finished",
        release,
        run_id,
        {"status": values["status"], "status_reason": values.get("status_reason")},
        outcome="success" if values["status"] == "passed" else "failure",
    )


def _score_values(
    session: Session, tenant_id: UUID, row: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The result columns of a running evaluation whose case runs have all ended; None while one
    has not."""
    dataset = repository.get_dataset(session, row["dataset_id"], tenant_id=tenant_id)
    assert dataset is not None  # the composite key restricts its deletion
    cases = {c["case_id"]: c for c in dataset["cases"]}
    outcomes = []
    for item in row["case_runs"]:
        outcome = run_outcome(session, tenant_id, UUID(item["run_id"]))
        if outcome is None or outcome.status not in state.TERMINAL:
            return None
        outcomes.append((item, outcome))
    results: list[dict[str, Any]] = []
    for item, outcome in outcomes:
        case_id = item["case_id"]
        reason = None
        if outcome.status != state.COMPLETED:
            reason = f"case {case_id}: the case run ended {outcome.status}"
        elif not outcome.output_kept:
            reason = f"case {case_id}: {OUTPUT_NOT_KEPT}"
        elif not outcome.cost_known:
            reason = f"case {case_id}: {COST_UNKNOWN}"
        if reason is not None:
            return {"status": "error", "status_reason": reason, "results": None}
        checks = assertions.evaluate(cases[case_id]["assertions"], outcome.output)
        latency = 0
        if outcome.started_at is not None and outcome.finished_at is not None:
            latency = int((outcome.finished_at - outcome.started_at).total_seconds() * 1000)
        results.append(
            {
                "case_id": case_id,
                "run_id": item["run_id"],
                "passed": all(c["passed"] for c in checks),
                "assertions": checks,
                "latency_ms": latency,
                "cost_usd": scoring.decimal_text(outcome.cost),
                "cost": outcome.cost,
            }
        )
    accuracy = scoring.accuracy(sum(1 for r in results if r["passed"]), len(results))
    p95 = scoring.p95_nearest_rank([r["latency_ms"] for r in results])
    total = sum((r.pop("cost") for r in results), Decimal(0))
    return {
        "status": scoring.verdict(accuracy, p95, total, row["thresholds"]),
        "accuracy": accuracy,
        "p95_latency_ms": p95,
        "total_cost_usd": scoring.decimal_text(total),
        "results": results,
    }


def score(
    session: Session, ctx: RequestContext, release: Mapping[str, Any], run_id: UUID
) -> Mapping[str, Any] | None:
    """Score the running evaluation `run_id` of `release` once its case runs have all ended:
    under the row lock, write its result columns and `finished_at` (no commit). The row as it
    stands afterwards; None for an unknown id."""
    tenant_id = release["tenant_id"]
    row = repository.get_run(session, run_id, tenant_id=tenant_id, lock=True)
    if row is None or row["status"] != "running":
        return row
    values = _score_values(session, tenant_id, row)
    if values is None:
        return row
    _finish(session, ctx, release, run_id, values)
    return repository.get_run(session, run_id, tenant_id=tenant_id)


def require_passing_evaluation(
    session: Session, release_id: UUID, *, ctx: RequestContext | None = None
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """(release, passing evaluation run) of the session's client (settled call 4): first scores
    the release's running evaluations, then finds a `passed` run whose `release_digest` is the
    release's digest and whose `dataset_digest` is its manifest's. No permission check (the
    caller checks its own) and no commit (the scores are written in the caller's transaction).
    An unknown or another client's release is `release not found`; no such run is
    `EvaluationRequired`. Withdrawal is not checked here."""
    tenant_id = repository.current_tenant_id(session)
    release = (
        release_record(session, release_id, tenant_id=tenant_id) if tenant_id is not None else None
    )
    if tenant_id is None or release is None:
        raise NotFound("release not found")
    context = ctx or RequestContext(request_id="evaluation-gate")
    for run_id in repository.running_run_ids(session, release_id, tenant_id=tenant_id):
        score(session, context, release, run_id)
    evaluation = release["manifest"].get("evaluation") or {}
    passing = repository.passing_run(
        session,
        release_id,
        tenant_id=tenant_id,
        release_digest=release["digest"],
        dataset_digest=evaluation.get("dataset_digest"),
    )
    if passing is None:
        raise EvaluationRequired("This release has no passing evaluation.")
    return release, passing


class EvaluationService:
    def __init__(
        self, scope: TenantScope, runtime: IntegrationRuntime, hooks: GovernanceHooks
    ) -> None:
        self.sc = scope
        self.runtime = runtime
        self.hooks = hooks
        self.artifacts = ArtifactService(scope, runtime)
        self.models = ModelService(scope, runtime)

    # ================================================================== datasets
    def _project(self, ws: str, proj: str) -> ResourceRef:
        project = tenancy.scope_ref_from_slugs(self.sc, ws, proj)
        return ResourceRef(
            self.sc.tenant_id,
            project.workspace_id,
            project.project_id,
            resource_type="evaluation_dataset",
        )

    def _active_scope(self, ws: str, proj: str) -> None:
        """Archived organizations, workspaces and projects take no new dataset."""
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")
        if tenancy.get_workspace(self.sc, ws).status != "active":
            raise ScopeArchived("workspace is archived")
        if tenancy.get_project(self.sc, ws, proj).status != "active":
            raise ScopeArchived("project is archived")

    @staticmethod
    def _dataset_out(row: Mapping[str, Any], ws: str, proj: str) -> s.DatasetOut:
        return s.DatasetOut(
            id=row["id"],
            workspace=ws,
            project=proj,
            name=row["name"],
            version_no=row["version_no"],
            cases=list(row["cases"]),
            digest=row["digest"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def list_datasets(
        self, ws: str, proj: str, *, limit: int, cursor: str | None
    ) -> Page[s.DatasetOut]:
        project = self._project(ws, proj)
        self.sc.pdp.require(DATASET_READ, project)
        assert project.project_id is not None
        rows = repository.list_datasets(
            self.sc.session,
            project.project_id,
            tenant_id=self.sc.tenant_id,
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._dataset_out(r, ws, proj) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def create_dataset(self, ws: str, proj: str, body: s.DatasetCreate) -> s.DatasetOut:
        project = self._project(ws, proj)
        self.sc.pdp.require(DATASET_WRITE, project)
        self._active_scope(ws, proj)
        assert project.project_id is not None and project.workspace_id is not None
        cases = body.stored_cases()
        digest = spec_hash(cases)
        session = self.sc.session
        for attempt in (1, 2):  # a concurrent create of the same version: retried once
            dataset_id = uuid7()
            version_no = repository.next_dataset_version_no(
                session, project.project_id, body.name, tenant_id=self.sc.tenant_id
            )
            try:
                repository.insert_dataset(
                    session,
                    {
                        "id": dataset_id,
                        "tenant_id": self.sc.tenant_id,
                        "workspace_id": project.workspace_id,
                        "project_id": project.project_id,
                        "name": body.name,
                        "version_no": version_no,
                        "cases": cases,
                        "digest": digest,
                        "created_by": self.sc.principal.principal_id,
                    },
                )
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                if attempt == 2:
                    raise Conflict(DATASET_VERSION_TAKEN) from exc
                continue
            break
        row = repository.get_dataset(session, dataset_id, tenant_id=self.sc.tenant_id)
        assert row is not None
        audit.record(
            session,
            self.sc.ctx,
            action="evaluation.dataset.created",
            tenant_id=self.sc.tenant_id,
            target_type="evaluation_dataset",
            target_id=dataset_id,
            target_label=f"evaluation_dataset:{body.name}",
            workspace_id=project.workspace_id,
            project_id=project.project_id,
            details={
                "name": body.name,
                "version_no": version_no,
                "cases": len(cases),
                "digest": digest,
            },
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise Conflict(DATASET_VERSION_TAKEN) from exc
        return self._dataset_out(row, ws, proj)

    def get_dataset(self, ws: str, proj: str, dataset_id: UUID) -> s.DatasetOut:
        project = self._project(ws, proj)
        assert project.project_id is not None and project.workspace_id is not None
        row = dataset_in_project(
            self.sc.session,
            self.sc.tenant_id,
            dataset_id,
            project.workspace_id,
            project.project_id,
        )
        if row is None:
            raise NotFound("dataset not found")
        ref = ResourceRef(
            self.sc.tenant_id,
            row["workspace_id"],
            row["project_id"],
            resource_type="evaluation_dataset",
            resource_id=row["id"],
        )
        try:
            self.sc.pdp.require(DATASET_READ, ref, hide_unless=DATASET_READ)
        except NotFound as exc:
            raise NotFound("dataset not found") from exc
        return self._dataset_out(row, ws, proj)

    # ================================================================== evaluation runs
    def _run_ref(self, release: Mapping[str, Any], run_id: UUID | None = None) -> ResourceRef:
        return ResourceRef(
            self.sc.tenant_id,
            release["workspace_id"],
            release["project_id"],
            resource_type="evaluation_run",
            resource_id=run_id,
        )

    def _release(self, release_id: UUID, action: str) -> Mapping[str, Any]:
        """The release, its scope its solution's, when the caller may `action` evaluation runs
        there; anything else (unknown, another client's, unreadable) is `release not found`."""
        release = release_record(self.sc.session, release_id, tenant_id=self.sc.tenant_id)
        if release is None:
            raise NotFound("release not found")
        try:
            self.sc.pdp.require(action, self._run_ref(release), hide_unless=RUN_READ)
        except NotFound as exc:
            raise NotFound("release not found") from exc
        return release

    def _scored(self, release: Mapping[str, Any], row: Mapping[str, Any]) -> Mapping[str, Any]:
        """`row`, scored first when it is running (committed when a score was written)."""
        if row["status"] != "running":
            return row
        scored = score(self.sc.session, self.sc.ctx, release, row["id"])
        assert scored is not None
        if scored["status"] != "running":
            self.sc.session.commit()
        else:
            self.sc.session.rollback()  # releases the row lock
        return scored

    def list_runs(
        self, release_id: UUID, *, limit: int, cursor: str | None
    ) -> Page[s.EvaluationRunOut]:
        release = self._release(release_id, RUN_READ)
        rows = repository.list_runs(
            self.sc.session,
            release_id,
            tenant_id=self.sc.tenant_id,
            after=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[_run_out(self._scored(release, r)) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def get_run(self, run_id: UUID) -> s.EvaluationRunOut:
        row = repository.get_run(self.sc.session, run_id, tenant_id=self.sc.tenant_id)
        release = (
            release_record(self.sc.session, row["release_id"], tenant_id=self.sc.tenant_id)
            if row is not None and row["tenant_id"] == self.sc.tenant_id
            else None
        )
        if (
            row is None
            or release is None
            or not self.sc.pdp.authorize(RUN_READ, self._run_ref(release, run_id)).allowed
        ):
            raise NotFound("evaluation run not found")
        return _run_out(self._scored(release, row))

    def _model_overrides(self, release: Mapping[str, Any]) -> tuple[dict[UUID, UUID], str | None]:
        """(pinned model id -> the client's own model of the binding's provider and served
        name, None) or ({}, why) for the first binding that names no model of this client
        (B-2707: never another model)."""
        overrides: dict[UUID, UUID] = {}
        bindings = release["manifest"]["bindings"]["model_bindings"]
        for pin in sorted(bindings):
            binding = bindings[pin]
            pinned = _uuid(pin)
            provider = _uuid(binding.get("provider_id"))
            own = (
                self.models.own_model_by_key(provider, str(binding.get("model")))
                if pinned is not None and provider is not None
                else None
            )
            if pinned is None or own is None:
                return {}, f"the model binding {pin} is not a model of this organization"
            overrides[pinned] = own
        return overrides, None

    def _case_body(
        self, release: Mapping[str, Any], slugs: tuple[str, str], case: Mapping[str, Any]
    ) -> RunCreate:
        entry = release["manifest"]["entrypoint"]
        version_id = UUID(entry["version_id"])
        access = self.artifacts.version_access(version_id)
        if access is None:
            raise NotFound("the release's entrypoint version is gone")
        value = case["input"]
        ws, proj = slugs
        if entry["kind"] == "pipeline":
            return RunCreate(
                kind="pipeline",
                pipeline_id=access.artifact_id,
                version_id=version_id,
                workspace=ws,
                project=proj,
                params=dict(value),
            )
        message = value.get("message")
        text = message if set(value) == {"message"} and isinstance(message, str) else None
        return RunCreate(
            kind="agent",
            agent_id=access.artifact_id,
            version_id=version_id,
            workspace=ws,
            project=proj,
            input=text if text is not None else canonical_json(value),
        )

    def _start_cases(
        self,
        release: Mapping[str, Any],
        cases: Sequence[Mapping[str, Any]],
        overrides: Mapping[UUID, UUID],
        max_cost: Decimal,
    ) -> tuple[list[dict[str, str]], str | None]:
        """Start one run per case as the caller: (case runs started, None) or, at the first case
        that cannot start, (the ones started before it, why)."""
        runs = RunService(self.sc, self.runtime, self.hooks)
        workspaces, projects = tenancy.scope_slug_map(
            self.sc, [release["workspace_id"]], [release["project_id"]]
        )
        slugs = (workspaces[release["workspace_id"]], projects[release["project_id"]])
        started: list[dict[str, str]] = []
        for case in cases:
            case_id = case["case_id"]
            try:
                body = self._case_body(release, slugs, case)
                run, _ = runs.create(body, None, model_overrides=overrides, max_cost=max_cost)
            except (AppError, ValueError) as exc:
                self.sc.session.rollback()
                detail = exc.detail if isinstance(exc, AppError) else "the case input is invalid"
                return started, f"case {case_id}: the case run could not start: {detail}"
            started.append({"case_id": case_id, "run_id": str(run.id)})
            if run.status == state.FAILED:  # a budget denial returns an already-failed run
                return started, f"case {case_id}: the case run was refused: {run.error_message}"
        return started, None

    def request(self, release_id: UUID, body: s.EvaluationRequest) -> s.EvaluationRunOut:
        del body  # `{}`: everything comes from the release's manifest
        release = self._release(release_id, RUN_REQUEST)
        session = self.sc.session
        if release["status"] == "withdrawn":
            raise ReleaseWithdrawn("The release is withdrawn.")
        evaluation = release["manifest"].get("evaluation") or {}
        dataset_id = _uuid(evaluation.get("dataset_id"))
        dataset = (
            dataset_in_project(
                session,
                self.sc.tenant_id,
                dataset_id,
                release["workspace_id"],
                release["project_id"],
            )
            if dataset_id is not None
            else None
        )
        if dataset is None or dataset["digest"] != evaluation.get("dataset_digest"):
            raise DatasetUnavailable("The release's evaluation dataset is not available.")
        runs = RunService(self.sc, self.runtime, self.hooks)
        if not runs.capture_full():
            raise FullCaptureRequired(
                "An evaluation needs full content capture in this organization."
            )
        run_id = uuid7()
        thresholds = dict(evaluation["thresholds"])
        repository.insert_run(
            session,
            {
                "id": run_id,
                "tenant_id": self.sc.tenant_id,
                "release_id": release_id,
                "release_digest": release["digest"],
                "dataset_id": dataset["id"],
                "dataset_digest": dataset["digest"],
                "thresholds": thresholds,
                "requested_by": self.sc.principal.principal_id,
            },
        )
        _audit_run(
            session,
            self.sc.ctx,
            "evaluation.run.requested",
            release,
            run_id,
            {
                "release_digest": release["digest"],
                "dataset_id": str(dataset["id"]),
                "dataset_digest": dataset["digest"],
                "cases": len(dataset["cases"]),
            },
        )
        session.commit()
        overrides, unbound = self._model_overrides(release)
        if unbound is not None:
            _finish(
                session,
                self.sc.ctx,
                release,
                run_id,
                {"status": "error", "status_reason": unbound, "results": None},
            )
            session.commit()
            return self._out_of(run_id)
        cases = list(dataset["cases"])
        cap = scoring.per_case_cost_cap(str(thresholds["max_total_cost_usd"]), len(cases))
        started, failure = self._start_cases(release, cases, overrides, cap)
        if failure is not None:
            for item in started:
                runs.cancel(UUID(item["run_id"]))
        repository.start_run(session, run_id, started, tenant_id=self.sc.tenant_id)
        if failure is not None:
            _finish(
                session,
                self.sc.ctx,
                release,
                run_id,
                {"status": "error", "status_reason": failure, "results": None},
            )
        session.commit()
        return self._out_of(run_id)

    def _out_of(self, run_id: UUID) -> s.EvaluationRunOut:
        row = repository.get_run(self.sc.session, run_id, tenant_id=self.sc.tenant_id)
        assert row is not None
        return _run_out(row)
