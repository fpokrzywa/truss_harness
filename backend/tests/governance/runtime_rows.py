"""Real runtime rows (runtime_0001) of one client for the governance reads that B-2105 turned on
(runtime isolation review 1, F1): finished agent and pipeline runs with model steps, an idle run,
approvals, old run artifacts and budget reservations. Written as the schema owner; the caller
commits. `n` sets how many of each a client gets, so a count that included another client's rows
shows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from tests._support.runs import add_agent_version, add_run
from tests.build.env_support import add_published

OLD_DAYS = 200  # older than every retention default (at most 90 days for run content)
HOLD = 10  # tokens held by each live reservation


@dataclass(frozen=True)
class RuntimeRows:
    runs: tuple[UUID, ...]  # finished agent runs, one model step each
    steps: tuple[UUID, ...]
    pipeline_runs: tuple[UUID, ...]
    idle_run: UUID  # pending, no usage events
    agent: UUID
    pipeline: UUID
    step_name: str

    @property
    def root_runs(self) -> int:
        return len(self.runs) + len(self.pipeline_runs) + 1


def _insert(s: Session, table: str, **values: Any) -> None:  # Any: column values
    names = list(values)
    s.execute(
        text(
            f"INSERT INTO shifde.{table} ({', '.join(names)}) "
            f"VALUES ({', '.join(':' + n for n in names)})"
        ),
        values,
    )


def seed_runtime(
    s: Session, tenant: UUID, ws: UUID, project: UUID, *, n: int, model: UUID
) -> RuntimeRows:
    """`n` finished agent runs (one `model_call` step each on `model`, which every client may
    share), `n` finished pipeline runs, one idle run; `n` pending approvals and one approved;
    `n` old output and `n` old intermediate artifacts plus a new one; `n` live holds of `HOLD`
    tokens and `n` stale holds."""
    now = utcnow()
    started, finished = now - timedelta(hours=2), now - timedelta(hours=1)
    agent, version = add_agent_version(s, tenant)
    pipeline, pipeline_version = add_published(s, tenant, "pipelines", f"pl-{uuid7().hex}", {})
    common = {"workspace_id": ws, "project_id": project, "status": "completed",
              "started_at": started, "finished_at": finished}  # fmt: skip
    runs = tuple(
        add_run(s, tenant, agent_id=agent, agent_version_id=version, **common) for _ in range(n)
    )
    pipeline_runs = tuple(
        add_run(
            s,
            tenant,
            kind="pipeline",
            pipeline_id=pipeline,
            pipeline_version_id=pipeline_version,
            **common,
        )  # fmt: skip
        for _ in range(n)
    )
    idle_run = add_run(s, tenant, workspace_id=ws, project_id=project)
    step_name = f"step-of-{tenant.hex[-8:]}"
    steps = []
    for run in runs:
        step = uuid7()
        _insert(s, "run_steps", id=step, tenant_id=tenant, run_id=run, seq=1, kind="model_call",
                name=step_name, status="succeeded", model_id=model, started_at=started,
                finished_at=finished)  # fmt: skip
        steps.append(step)
    approval = {"tenant_id": tenant, "run_id": runs[0], "workspace_id": ws,
                "project_id": project, "reason": "tool_side_effect",
                "expires_at": now + timedelta(hours=1)}  # fmt: skip
    for i in range(n):
        _insert(s, "approvals", id=uuid7(), call_ref=f"c{i}", requested_by=uuid7(), **approval)
    decider = uuid7()
    _insert(s, "approvals", id=uuid7(), call_ref="decided", requested_by=decider,
            status="approved", decision="approve", decided_by=decider, decided_at=now,
            **approval)  # fmt: skip
    old = now - timedelta(days=OLD_DAYS)
    for kind, created in (("output", old), ("intermediate", old), ("output", now)):
        for _ in range(n if created == old else 1):
            _insert(s, "run_artifacts", id=uuid7(), tenant_id=tenant, run_id=runs[0], kind=kind,
                    size_bytes=2, content="{}", sha256="x", created_at=created)  # fmt: skip
    for i in range(n):
        for key, expires in ((f"live-{i}", now + timedelta(hours=1)), (f"stale-{i}", old)):
            _insert(s, "budget_reservations", id=uuid7(), tenant_id=tenant, run_id=runs[0],
                    step_key=key, budget_ids="{}", amount=HOLD, status="held",
                    expires_at=expires)  # fmt: skip
    return RuntimeRows(runs, tuple(steps), pipeline_runs, idle_run, agent, pipeline, step_name)
