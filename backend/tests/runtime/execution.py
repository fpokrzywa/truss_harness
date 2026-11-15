"""Seed and helpers for the runtime part 4 tests (`context.py`, `settle.py`, `gate.py`, and the
run's `Env`, B-1933): per client, a member who starts runs, an agent version that pins a prompt
which composes another prompt (transitive pins), an unpinned prompt, another agent, and runs in
the states the three files act on. Written as the schema owner and committed."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.runtime import IntegrationRuntime
from shifde.build import repository as build_repo
from shifde.core.clock import utcnow
from shifde.core.context import Principal
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from shifde.runtime.governance import PolicyBudgetHooks
from tests._support.runs import add_run
from tests.build.env_support import add_published
from tests.build.service_support import REGISTRY, add_person
from tests.runtime.conftest import insert


@dataclass(frozen=True)
class Seed:
    tenant: UUID
    user: UUID
    agent: UUID  # artifact of `agent_version`
    agent_version: UUID  # pins `prompt`, which composes `composed`
    prompt: UUID
    composed: UUID
    unpinned: UUID  # a published prompt of the same client, pinned by nothing
    other_agent: UUID
    other_version: UUID
    token: UUID  # lease token of `running`
    running: UUID  # running agent run of `agent_version`, initiated by `user`
    child: UUID  # pending child of `running` with a started write
    waiting: UUID  # parent waiting for its children (`child_runs`), one finished child
    finished_child: UUID
    pipeline: UUID  # failed pipeline run
    pipeline_child: UUID  # running agent child of `pipeline`, root `pipeline`
    ended: UUID  # cancelled run with a started write and a pending tool approval
    other_run: UUID  # run of `other_agent`
    unpinned_art: UUID  # artifact of `unpinned`


def _write_in_flight(s: Session, tenant: UUID, run: UUID, tool_version: UUID) -> None:
    ws, proj = s.execute(
        text("SELECT workspace_id, project_id FROM shifde.runs WHERE id = :r"), {"r": run}
    ).one()
    invocation = uuid7()
    insert(s, "tool_invocations", id=invocation, tenant_id=tenant, run_id=run, call_ref="w1",
           tool_version_id=tool_version, side_effect_class="write_non_idempotent",
           idempotency_key=f"k-{invocation}", status="started", request_hash="h",
           arguments='{"item": "x"}')  # fmt: skip
    insert(s, "run_steps", id=uuid7(), tenant_id=tenant, run_id=run, seq=1, kind="tool_call",
           name="t", status="running", tool_invocation_id=invocation,
           tool_version_id=tool_version)  # fmt: skip
    insert(s, "approvals", id=uuid7(), tenant_id=tenant, run_id=run, workspace_id=ws,
           project_id=proj, call_ref="w1", reason="tool_side_effect", requested_by=uuid7(),
           expires_at=utcnow() + timedelta(hours=1))  # fmt: skip


def seed_client(s: Session, tenant: UUID) -> Seed:
    user = add_person(s, tenant, "runner", "tenant_administrator")
    _, composed = add_published(s, tenant, "prompts", f"composed-{uuid7().hex}", {})
    _, prompt = add_published(s, tenant, "prompts", f"prompt-{uuid7().hex}", {})
    unpinned_art, unpinned = add_published(s, tenant, "prompts", f"unpinned-{uuid7().hex}", {})
    agent, agent_version = add_published(s, tenant, "agents", f"agent-{uuid7().hex}", {})
    # a latest published version for each, so `latest()` has something to refuse in a run's env
    # (runtime isolation review 3 item 1)
    for artifact in (agent, unpinned_art):
        build_repo.refresh_latest_published(s, tenant, artifact)
    other_agent, other_version = add_published(s, tenant, "agents", f"other-{uuid7().hex}", {})
    build_repo.insert_pins(s, tenant, prompt, [(composed, "composed_prompt")])
    build_repo.insert_pins(s, tenant, agent_version, [(prompt, "prompt")])
    _, tool_version = add_published(s, tenant, "tools", f"tool-{uuid7().hex}", {})
    pipe_id, pipe_version = add_published(s, tenant, "pipelines", f"pipe-{uuid7().hex}", {})
    target = {"agent_id": agent, "agent_version_id": agent_version}
    token = uuid7()
    running = add_run(s, tenant, status="running", lease_token=token, started_at=utcnow(),
                      initiated_by_principal_id=user, **target)  # fmt: skip
    child = add_run(s, tenant, parent_run_id=running, root_run_id=running, depth=1, **target)
    _write_in_flight(s, tenant, child, tool_version)
    waiting = add_run(s, tenant, status="waiting_for_approval", waiting_reason="child_runs",
                      **target)  # fmt: skip
    finished_child = add_run(s, tenant, parent_run_id=waiting, root_run_id=waiting, depth=1,
                             status="completed", finished_at=utcnow(), **target)  # fmt: skip
    pipeline = add_run(s, tenant, kind="pipeline", pipeline_id=pipe_id,
                       pipeline_version_id=pipe_version, status="failed",
                       finished_at=utcnow())  # fmt: skip
    pipeline_child = add_run(s, tenant, parent_run_id=pipeline, root_run_id=pipeline, depth=1,
                             status="running", lease_token=token, started_at=utcnow(),
                             initiated_by_principal_id=user, **target)  # fmt: skip
    ended = add_run(s, tenant, status="cancelled", finished_at=utcnow(),
                    error_class="cancelled", **target)  # fmt: skip
    _write_in_flight(s, tenant, ended, tool_version)
    other_run = add_run(s, tenant, agent_id=other_agent, agent_version_id=other_version)
    return Seed(tenant, user, agent, agent_version, prompt, composed, unpinned, other_agent,
                other_version, token, running, child, waiting, finished_child, pipeline,
                pipeline_child, ended, other_run, unpinned_art)  # fmt: skip


def run_row(s: Session, tenant: UUID, run: UUID) -> dict[str, Any]:  # Any: run row
    row = repo.get_run(s, tenant, run)
    assert row is not None, run
    return dict(row)


def run_context(
    s: Session,
    settings: Settings,
    run: dict[str, Any],  # Any: run row
    *,
    token: UUID | None = None,
) -> RunContext:
    """A worker's context for `run` on `s` (the lease token defaults to the row's)."""
    principal = Principal(
        user_id=run["initiated_by_principal_id"], email="runner@shi.test", display_name="runner"
    )
    return RunContext(
        session=s,
        settings=settings,
        runtime=IntegrationRuntime(settings=settings, registry=REGISTRY),
        hooks=PolicyBudgetHooks(),
        run=run,
        token=token or run["lease_token"] or uuid7(),
        worker_id="w-test",
        stop=threading.Event(),
        lease_lost=threading.Event(),
        principal=principal,
        tenant_header=("slug", "name", "active"),
    )


def client_state(o: Session, tenant: UUID) -> tuple[Any, ...]:  # Any: row values
    """Everything the RT-4 functions could change in `tenant`'s rows (read as the owner)."""
    queries = (
        "SELECT id, status, error_class, waiting_reason, cancel_requested_at, state_version, "
        "event_seq FROM shifde.runs WHERE tenant_id = :t ORDER BY id",
        "SELECT id, status, reason FROM shifde.approvals WHERE tenant_id = :t ORDER BY id",
        "SELECT id, status FROM shifde.tool_invocations WHERE tenant_id = :t ORDER BY id",
        "SELECT id, status FROM shifde.run_steps WHERE tenant_id = :t ORDER BY id",
        "SELECT id, status FROM shifde.jobs WHERE tenant_id = :t ORDER BY id",
        "SELECT count(*) FROM shifde.run_events WHERE tenant_id = :t",
        "SELECT count(*) FROM shifde.policy_decisions WHERE tenant_id = :t",
        "SELECT count(*) FROM shifde.audit_events WHERE tenant_id = :t",
        "SELECT count(*) FROM shifde.budget_reservations WHERE tenant_id = :t",
    )
    o.rollback()
    return tuple(tuple(map(tuple, o.execute(text(q), {"t": tenant}).all())) for q in queries)
