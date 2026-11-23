"""`audit_events` is indexed for the model-provider job result lookup (issue #183, B-2993).

`audit.repository.find_event` has no time bound (B-2861): it filters one client's events on
action, target and correlation id. `audit_0002` adds `(tenant_id, correlation_id)`; these tests
pin the index definition and show the planner uses it for the lookup once a client has a long
history, so the lookup no longer scans that client's whole audit log.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.audit import repository
from shifde.core.ids import uuid7

INDEX = "ix_audit_events__tenant_id_correlation_id"

LOOKUP = (
    "SELECT * FROM shifde.audit_events WHERE tenant_id = :t AND action = :a AND target_id = :g "
    "AND correlation_id = :c ORDER BY occurred_at DESC LIMIT 1"
)


def _index_names(plan: Any) -> set[str]:  # Any: EXPLAIN (FORMAT JSON) node
    names: set[str] = set()
    if isinstance(plan, dict):
        if "Index Name" in plan:
            names.add(plan["Index Name"])
        for value in plan.values():
            names |= _index_names(value)
    elif isinstance(plan, list):
        for item in plan:
            names |= _index_names(item)
    return names


def test_the_correlation_index_exists_and_leads_with_tenant_id(owner_session: Session) -> None:
    definition = owner_session.execute(
        text(
            "SELECT indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = 'shifde' AND indexname = :i"
        ),
        {"i": INDEX},
    ).scalar_one()
    assert definition == (
        f"CREATE INDEX {INDEX} ON shifde.audit_events USING btree (tenant_id, correlation_id)"
    )


def test_the_job_result_lookup_uses_the_correlation_index(owner_session: Session) -> None:
    tenant, other = uuid7(), uuid7()
    # a long history for the client (and another client), each event its own correlation id
    for t in (tenant, other):
        owner_session.execute(
            text(
                "INSERT INTO shifde.audit_events (id, tenant_id, occurred_at, actor_type, action, "
                "target_type, target_id, outcome, correlation_id) "
                "SELECT gen_random_uuid(), :t, now() - make_interval(secs => g), 'system', "
                "'models.job.completed', 'model_job', 'job-' || g, 'success', 'corr-' || g "
                "FROM generate_series(1, 4000) AS g"
            ),
            {"t": t},
        )
    owner_session.execute(text("ANALYZE shifde.audit_events"))
    params = {"t": tenant, "a": "models.job.completed", "g": "job-17", "c": "corr-17"}
    plan = owner_session.execute(text(f"EXPLAIN (FORMAT JSON) {LOOKUP}"), params).scalar_one()
    if isinstance(plan, str):
        plan = json.loads(plan)
    assert INDEX in _index_names(plan), plan

    found = repository.find_event(
        owner_session,
        tenant_id=tenant,
        action="models.job.completed",
        target_id="job-17",
        correlation_id="corr-17",
    )
    assert found is not None and found.tenant_id == tenant and found.correlation_id == "corr-17"
    # the same correlation id of another client is not this client's event
    assert (
        repository.find_event(
            owner_session,
            tenant_id=uuid7(),
            action="models.job.completed",
            target_id="job-17",
            correlation_id="corr-17",
        )
        is None
    )
    owner_session.rollback()
