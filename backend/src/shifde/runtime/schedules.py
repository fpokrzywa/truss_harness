"""Pipeline schedules: the extension point of P6 (docs/api/PHASE_6_CONTRACT.md section 4).

The table, the service interface and the next-run computation exist and are tested; cron
EXECUTION (a `scheduler.tick` run by the leader worker as the schedule's service account) is not
enabled in the MVP (OD-8). Until the deployment sets SHIFDE_FEATURE_PIPELINE_SCHEDULES=true every
schedule endpoint answers 501 `scheduling_not_enabled`.

There is no due-schedule claim in this file (SETH has none: OD-8). Every statement names the
scope's client next to row-level security (B-2132); the service account a schedule runs as is
read in the client and guarded by its own bindings in the client (`iam.guard_bindings`, B-1601).
A future cron runner must claim due schedules with the schedule row's own `tenant_id` and start
the run in that client only (carry_forward, runtime).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any  # Any: rows / params
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.admin import iam
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.build.issues import schema_errors
from shifde.build.service import ArtifactService
from shifde.core.clock import utcnow
from shifde.core.cron import Cron, CronError
from shifde.core.errors import Conflict, NotFound, SchedulingNotEnabled, ValidationFailed
from shifde.core.ids import uuid7
from shifde.runtime import schemas as s
from shifde.tenancy.scope import TenantScope

COLUMNS = (
    "id, pipeline_id, version_policy, pipeline_version_id, cron, timezone, params, "
    "run_as_service_account_id, status, next_run_at, created_at"
)


class ScheduleService:
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime) -> None:
        self.sc = scope
        self.runtime = runtime
        self.artifacts = ArtifactService(scope, runtime)

    @property
    def session(self) -> Session:
        return self.sc.session

    def _enabled(self) -> None:
        if not self.runtime.settings.feature_pipeline_schedules:
            raise SchedulingNotEnabled(
                "pipeline schedules are not enabled in this deployment "
                "(SHIFDE_FEATURE_PIPELINE_SCHEDULES)"
            )

    @staticmethod
    def _out(row: Mapping[Any, Any]) -> s.ScheduleOut:  # Any: row
        return s.ScheduleOut(
            id=row["id"],
            pipeline_id=row["pipeline_id"],
            version_policy=row["version_policy"],
            pipeline_version_id=row["pipeline_version_id"],
            cron=row["cron"],
            timezone=row["timezone"],
            params=dict(row["params"]),
            run_as_service_account_id=row["run_as_service_account_id"],
            status=row["status"],
            next_run_at=row["next_run_at"],
            created_at=row["created_at"],
        )

    def list(self, pipeline_id: UUID) -> list[s.ScheduleOut]:
        self._enabled()
        art = self.artifacts.artifact("pipelines", pipeline_id)  # pipeline:read, else 404
        rows = (
            self.session.execute(
                text(
                    "SELECT " + COLUMNS + " FROM shifde.pipeline_schedules "
                    "WHERE tenant_id = :t AND pipeline_id = :p ORDER BY id"
                ),
                {"t": self.sc.tenant_id, "p": art["id"]},
            )
            .mappings()
            .all()
        )
        return [self._out(r) for r in rows]

    def _check_params(self, art: Mapping[str, Any], body: s.ScheduleCreate) -> None:  # Any: row
        """The schedule's params must satisfy the pipeline's parameter schema (the version it
        will run: the pinned one, else the latest published, else the draft)."""
        version_id = body.pipeline_version_id or art["latest_published_version_id"]
        spec = self.artifacts.version_spec(version_id) if version_id else None
        if spec is None:
            draft = self.artifacts.draft_of(art)
            spec = dict(draft["spec"]) if draft is not None else None
        schema = (spec or {}).get("params")
        if isinstance(schema, dict):
            problems = schema_errors(schema, body.params)
            if problems:
                raise ValidationFailed(
                    "the schedule params are invalid",
                    errors=[
                        {
                            "loc": ["body", "params", *[str(p) for p in path]],
                            "msg": message,
                            "type": "invalid_params",
                        }
                        for path, message in problems
                    ],
                )

    def _check_service_account(self, sa_id: UUID) -> None:
        """Running as a service account hands out ITS permissions: the caller must hold every one
        of them (the escalation guard used for suspending / re-enabling subjects)."""
        found = self.session.execute(
            text("SELECT 1 FROM shifde.service_accounts WHERE tenant_id = :t AND id = :i"),
            {"t": self.sc.tenant_id, "i": sa_id},
        ).first()
        if found is None:
            raise NotFound("service account not found")
        # SETH's `guard_target_privileges(scope, "service_account", ...)` is the bindings loop
        # `iam.guard_bindings` here (B-1601): the account's own bindings in this client
        bindings = authz.bindings_of_subject(
            self.session, self.sc.tenant_id, "service_account", [sa_id]
        )
        iam.guard_bindings(self.sc, bindings, action="schedule runs as this service account")

    def create(self, pipeline_id: UUID, body: s.ScheduleCreate) -> s.ScheduleOut:
        self._enabled()
        art = self.artifacts.artifact("pipelines", pipeline_id, "update")
        try:
            cron = Cron.parse(body.cron)
            next_run = cron.next_after(utcnow(), body.timezone) if body.status == "active" else None
            if body.status == "paused":
                cron.next_after(utcnow(), body.timezone)  # validates the time zone too
        except CronError as exc:
            raise ValidationFailed(str(exc)) from exc
        if (body.version_policy == "pinned") != (body.pipeline_version_id is not None):
            raise ValidationFailed("pipeline_version_id is required exactly for a pinned schedule")
        if body.pipeline_version_id is not None:
            self.artifacts.version_of(art, body.pipeline_version_id)  # 404 unless of this pipeline
        self._check_params(art, body)
        self._check_service_account(body.run_as_service_account_id)
        schedule_id = uuid7()
        try:
            self.session.execute(
                text(
                    "INSERT INTO shifde.pipeline_schedules (id, tenant_id, pipeline_id, "
                    "version_policy, pipeline_version_id, cron, timezone, params, "
                    "run_as_service_account_id, status, next_run_at, created_by) VALUES "
                    "(:id, :t, :p, :vp, :pv, :cron, :tz, CAST(:params AS jsonb), :sa, :st, :next, "
                    ":by)"
                ),
                {
                    "id": schedule_id,
                    "t": self.sc.tenant_id,
                    "p": art["id"],
                    "vp": body.version_policy,
                    "pv": body.pipeline_version_id,
                    "cron": body.cron,
                    "tz": body.timezone,
                    "params": json.dumps(body.params),
                    "sa": body.run_as_service_account_id,
                    "st": body.status,
                    "next": next_run,
                    "by": self.sc.principal.principal_id,
                },
            )
        except IntegrityError as exc:
            self.session.rollback()
            raise NotFound("service account not found") from exc
        audit.record(
            self.session,
            self.sc.ctx,
            action="pipeline.schedule_created",
            tenant_id=self.sc.tenant_id,
            target_type="pipeline_schedule",
            target_id=schedule_id,
            workspace_id=art["workspace_id"],
            project_id=art["project_id"],
            details={"pipeline_id": str(art["id"]), "cron": body.cron, "status": body.status},
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict("the schedule could not be created") from exc
        row = (
            self.session.execute(
                text(
                    f"SELECT {COLUMNS} FROM shifde.pipeline_schedules "
                    "WHERE tenant_id = :t AND id = :id"
                ),
                {"t": self.sc.tenant_id, "id": schedule_id},
            )
            .mappings()
            .one()
        )
        return self._out(row)
