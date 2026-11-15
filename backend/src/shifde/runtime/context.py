"""Per-advance execution context: tenant scope of the initiating principal, lease fencing,
cooperative checkpoints (cancel / deadline / shutdown / lease loss) and audit helpers.

The client is the run row's `tenant_id`; every repository call names it (B-2115), so a run id of
another client (a forged parent id, a reused context) answers as an unknown run. The run's
unchecked `Env` (the Execution Gate already authorized the run) is `env`: the build service's
`run_of` counterpart of `approval_of` (B-1933), never a visibility flag."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any  # Any: audit details / JSON
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.build.env import Env
from shifde.build.service import ArtifactService
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from shifde.runtime import state
from shifde.runtime.governance import GovernanceHooks
from shifde.tenancy import service as tenancy
from shifde.tenancy.scope import TenantScope


@dataclass(frozen=True)
class RunIdentity:
    """What `execution_principal` needs from a run row (authz.execution.RunRef)."""

    tenant_id: UUID
    initiated_by_principal_id: UUID
    principal_type: str
    api_key_id: UUID | None


class RunContext:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        runtime: IntegrationRuntime,
        hooks: GovernanceHooks,
        run: Mapping[str, Any],  # Any: run row
        token: UUID,
        worker_id: str,
        stop: threading.Event,
        lease_lost: threading.Event,
        principal: Principal,
        tenant_header: tuple[str, str, str],
        db: Database | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime
        self.hooks = hooks
        self.run: Mapping[str, Any] = run
        self.token = token
        self.worker_id = worker_id
        self.stop = stop
        self.lease_lost = lease_lost
        self.principal = principal
        # the worker's database: a model call's usage row is written in its own committed
        # transaction the moment the provider answers (carry_forward Item 11); without one it is
        # written in the step's transaction
        self.db = db
        self.tenant_id: UUID = run["tenant_id"]
        self.scope = TenantScope(
            session=session,
            ctx=RequestContext(request_id=run["trace_id"], principal=principal),
            principal=principal,
            tenant_id=self.tenant_id,
            tenant_slug=tenant_header[0],
            tenant_name=tenant_header[1],
            tenant_status=tenant_header[2],
            pdp=authz.new_pdp(session, principal, self.tenant_id),
        )
        self.artifacts = ArtifactService(self.scope, runtime)

    # ------------------------------------------------------------------ identity
    @property
    def run_id(self) -> UUID:
        rid: UUID = self.run["id"]
        return rid

    @property
    def identity(self) -> RunIdentity:
        return RunIdentity(
            self.tenant_id,
            self.run["initiated_by_principal_id"],
            self.run["principal_type"],
            self.run["api_key_id"],
        )

    def set_principal(self, principal: Principal) -> None:
        """Replace the principal AND its PDP (fresh grants) on the shared scope, so the services
        built on it (models, connections, artifacts) decide with current permissions."""
        self.principal = principal
        self.scope.principal = principal
        self.scope.ctx.principal = principal
        self.scope.pdp = authz.new_pdp(self.session, principal, self.tenant_id)

    def run_ref(self) -> ResourceRef:
        return ResourceRef(self.tenant_id, self.run["workspace_id"], self.run["project_id"])

    def env(self, art: Mapping[str, Any]) -> Env:  # Any: artifact row
        """The validators' `Env` for executing `art`, the artifact of this run's own pinned
        version: the build service re-reads the run with this client's id and answers only for
        that version and what it pins (B-1933). Another client's run, another artifact or another
        client's `art` is the unknown-id answer (`NotFound`)."""
        return self.artifacts.env(art, run_of=self.run)

    def reload_run(self) -> Mapping[str, Any]:
        fresh = repo.get_run(self.session, self.tenant_id, self.run_id)
        if fresh is not None:
            self.run = fresh
        return self.run

    # ------------------------------------------------------------------ time
    @property
    def deadline(self) -> datetime:
        deadline: datetime = self.run["deadline_at"]
        return deadline

    def remaining(self) -> float:
        return (self.deadline - utcnow()).total_seconds()

    # ------------------------------------------------------------------ control flow
    def checkpoint(self) -> None:
        """Cooperative control point (before every gate and between steps)."""
        if self.stop.is_set():
            raise state.Shutdown()
        if self.lease_lost.is_set():
            raise state.LeaseLost()
        ctl = repo.control_state(self.session, self.tenant_id, self.run_id)
        self.session.commit()  # never hold a snapshot / locks across a long call
        if ctl is None or ctl["status"] != state.RUNNING or ctl["lease_token"] != self.token:
            raise state.LeaseLost()
        if ctl["cancel_requested_at"] is not None:
            raise state.Cancelled()
        parent_id = self.run["parent_run_id"]
        if parent_id is not None:
            parent = repo.control_state(self.session, self.tenant_id, parent_id)
            self.session.commit()
            kind = self.run["kind"]
            if (
                kind == "agent"
                and parent is not None
                and parent["status"] in (state.FAILED, state.CANCELLED, state.TIMED_OUT)
                and self._parent_is_pipeline(parent_id)
            ):
                raise state.Cancelled()
        if utcnow() >= ctl["deadline_at"]:
            raise state.TimedOut()

    def _parent_is_pipeline(self, parent_id: UUID) -> bool:
        parent = repo.get_run(self.session, self.tenant_id, parent_id)
        self.session.commit()
        return parent is not None and parent["kind"] == "pipeline"

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep (cancel / deadline / shutdown are honoured every 0.25 s)."""
        end = time.monotonic() + max(seconds, 0)
        while True:
            self.checkpoint()
            left = end - time.monotonic()
            if left <= 0:
                return
            if self.remaining() <= left:
                raise state.TimedOut()
            self.stop.wait(min(0.25, left))

    @contextmanager
    def txn(self) -> Iterator[None]:
        """One fenced write transaction: the run row is locked iff this worker still holds the
        lease. Commits on success, rolls back on any error."""
        if self.lease_lost.is_set():
            raise state.LeaseLost()
        if not repo.fence(self.session, self.tenant_id, self.run_id, self.token):
            self.session.rollback()
            raise state.LeaseLost()
        try:
            yield
            self.session.commit()
        except BaseException:
            self.session.rollback()
            raise

    # ------------------------------------------------------------------ audit
    def audit(
        self,
        action: str,
        details: Mapping[str, Any] | None = None,  # Any: audit details
        *,
        outcome: str = "success",
        commit: bool = True,
    ) -> None:
        if commit:  # lease-fenced: a worker that lost the run cannot append to its history
            with self.txn():
                self.audit(action, details, outcome=outcome, commit=False)
            return
        run = self.run
        audit.record(
            self.session,
            self.scope.ctx,
            action=action,
            outcome=outcome,
            tenant_id=self.tenant_id,
            actor_id=run["initiated_by_principal_id"],
            actor_label=self.principal.email,
            auth_method="execution",
            target_type="run",
            target_id=run["id"],
            target_label=f"run:{run['id']}",
            workspace_id=run["workspace_id"],
            project_id=run["project_id"],
            details={
                "run_id": str(run["id"]),
                "kind": run["kind"],
                "trace_id": run["trace_id"],
                **dict(details or {}),
            },
        )


def tenant_header(session: Session, tenant_id: UUID) -> tuple[str, str, str]:
    header = tenancy.tenant_header(session, tenant_id)
    if header is None:
        raise state.RunFailed(state.AUTHZ_DENIED, "the tenant is not available")
    return header
