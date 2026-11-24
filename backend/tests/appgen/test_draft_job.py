"""SP5-2 (design 5.2, 5.6 and 9.3): the `appgen.draft` job.

`[[appgen-sample]]` drafts revision 1 equal to `SAMPLE_DRAFT` with a settled budget hold and a
`model_call` usage row; on a Snowflake connection (SP6-4) `[[appgen-sample-snowflake]]` drafts
revision 1 equal to `SNOWFLAKE_SAMPLE_DRAFT` with no diagnostic; `[[appgen-invalid]]` ends `failed`/`invalid_model_output`. The SHI
provider switched off refuses the create as `models_admin` refuses an unknown model, with no job.
An exhausted budget ends `failed` with the budget code and no provider call. A worker that dies
after committing `drafting` leaves the request `drafting`: the job run again records
`model_call_unknown` without a second provider call, and the invocation-hold sweep settles the
hold `settled_unknown`. A lost permission ends `access_revoked`, a retired connection
`connection_unavailable`. The prompt holds column names and no sample row value. A `Worker`
built with `shifde.worker_main.job_handlers()` runs the job.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import worker_main
from shifde.adapters.model_providers import FakeProvider
from shifde.appgen import service as appgen
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.ledger import reservations
from shifde.models_admin.repository import SHI_PROVIDER_ID
from shifde.runtime.worker import Worker
from tests._support.appgen_fixtures import SAMPLE_DRAFT, SNOWFLAKE_SAMPLE_DRAFT
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.p3util import ADMIN, A
from tests._support.p5util import Hdr, limited_user, user_session
from tests.appgen.helpers import (
    INVALID,
    SF_SAMPLE,
    SF_TABLES,
    Calls,
    body,
    created,
    databricks,
    draft_jobs,
    drafted,
    drafting_model,
    get,
    job_of,
    record_calls,
    run_job,
    snowflake,
    wire,
    wire_snowflake,
)
from tests.conftest import CLEAN_TABLES
from tests.ledger.util import add_budget
from tests.tenancy.conftest import app_session

for _table in ("budgets", "budget_reservations", "usage_events"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)

SAMPLE_VALUES = ("Sample Customer", "North", "South", "175.5", "2026-02-17")
PERMISSIONS = [
    "workspace:read",
    "project:read",
    "connection:read",
    "model:read",
    "model:invoke",
    "appgen:request:read",
    "appgen:request:write",
]


@pytest.fixture
def admin(client: TestClient, world: World) -> Hdr:
    wire(client)
    return user_session(client, world.passwords, ADMIN)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    return record_calls(monkeypatch)


def _holds(owner: Session) -> list[tuple[Any, ...]]:  # Any: row values
    owner.rollback()
    rows = owner.execute(
        text(
            "SELECT status, invocation_id IS NOT NULL, provider_request_at IS NOT NULL "
            "FROM shifde.budget_reservations ORDER BY id"
        )
    ).all()
    owner.rollback()
    return [tuple(r) for r in rows]


def _reason(c: TestClient, h: Hdr, request_id: str) -> tuple[str, str | None]:
    out = get(c, h, request_id)
    return out["status"], out["status_reason"]


def test_a_sample_brief_drafts_revision_1_with_a_settled_hold(
    admin: Hdr, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    add_budget(owner_session, tenant_id(owner_session, "demo-a"), amount="1000000")
    owner_session.commit()
    cid, model = databricks(c, admin), drafting_model(c, admin)
    out = drafted(c, admin, owner_session, body(cid, model))
    [revision] = out["revisions"]
    assert revision["revision_no"] == 1 and revision["source"] == "model"
    assert revision["draft"] == SAMPLE_DRAFT and revision["validation"] == []
    assert revision["created_by"] == out["requested_by"]
    assert len(calls.messages) == 1
    assert _holds(owner_session) == [("settled", True, True)]
    usage = owner_session.execute(
        text("SELECT event_type, model_id FROM shifde.usage_events")
    ).all()
    assert [(u[0], str(u[1])) for u in usage] == [("model_call", model)]
    jobs = draft_jobs(owner_session)
    assert [j[1] for j in jobs] == ["done"]
    owner_session.rollback()
    actions = owner_session.execute(
        text("SELECT action FROM shifde.audit_events WHERE target_id = :r ORDER BY chain_seq"),
        {"r": out["id"]},
    ).all()
    assert [a[0] for a in actions] == [
        "appgen.request.created",
        "appgen.request.drafting",
        "appgen.request.drafted",
    ]


def test_a_snowflake_sample_brief_drafts_revision_1_with_no_diagnostic(
    admin: Hdr, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    wire_snowflake(c)
    cid, model = snowflake(c, admin), drafting_model(c, admin)
    out = drafted(
        c, admin, owner_session, body(cid, model, f"Sales app {SF_SAMPLE}", tables=SF_TABLES)
    )
    [revision] = out["revisions"]
    assert revision["revision_no"] == 1 and revision["source"] == "model"
    assert revision["draft"] == SNOWFLAKE_SAMPLE_DRAFT and revision["validation"] == []
    assert len(calls.messages) == 1
    assert [j[1] for j in draft_jobs(owner_session)] == ["done"]


def test_an_invalid_reply_fails_the_request(
    admin: Hdr, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    made = created(c, admin, body(cid, model, brief=f"broken {INVALID}"))
    assert run_job(c, owner_session, made["id"])
    assert _reason(c, admin, made["id"]) == ("failed", "invalid_model_output")
    assert get(c, admin, made["id"])["revisions"] == [] and len(calls.messages) == 1
    owner_session.rollback()
    detail = owner_session.execute(
        text(
            "SELECT details FROM shifde.audit_events WHERE target_id = :r "
            "AND action = 'appgen.request.failed'"
        ),
        {"r": made["id"]},
    ).scalar_one()
    assert detail["reason"] == "invalid_model_output" and detail["messages"]


def test_the_shi_switch_off_refuses_the_create_and_queues_nothing(
    admin: Hdr, owner_session: Session
) -> None:
    c = admin.client
    cid = databricks(c, admin)
    shared = uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, settings, "
            "status) VALUES (:p, NULL, 'SHI', 'fake', '{}'::jsonb, 'active') "
            "ON CONFLICT (id) DO UPDATE SET status = 'active'"
        ),
        {"p": SHI_PROVIDER_ID},
    )
    owner_session.execute(
        text(
            "INSERT INTO shifde.models (id, tenant_id, provider_id, model_key, display_name, "
            "capabilities, lifecycle) VALUES (:m, NULL, :p, 'shi-chat', 'SHI chat', "
            "ARRAY['chat'], 'active')"
        ),
        {"m": shared, "p": SHI_PROVIDER_ID},
    )
    owner_session.commit()
    r = c.post(f"{A}/workspaces/main/projects/sample/app-requests", headers=admin,
               json=body(cid, str(shared)))  # fmt: skip
    unknown = c.get(f"{A}/models/{uuid7()}", headers=admin)
    assert r.status_code == 404 and r.json()["detail"] == unknown.json()["detail"], r.text
    assert draft_jobs(owner_session) == []


def test_an_exhausted_budget_fails_without_a_provider_call(
    admin: Hdr, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    add_budget(owner_session, tenant_id(owner_session, "demo-a"), amount="10")
    owner_session.commit()
    cid, model = databricks(c, admin), drafting_model(c, admin)
    made = created(c, admin, body(cid, model))
    assert run_job(c, owner_session, made["id"])
    assert _reason(c, admin, made["id"]) == ("failed", "budget_denied")
    assert calls.messages == [] and _holds(owner_session) == []


class _Crash(BaseException):
    """The worker process dies in the provider call (nothing after it runs)."""


def test_a_crash_after_drafting_records_model_call_unknown_once(
    admin: Hdr,
    owner_session: Session,
    app_db: Database,
    calls: Calls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = admin.client
    tenant = tenant_id(owner_session, "demo-a")
    add_budget(owner_session, tenant, amount="1000000")
    owner_session.commit()
    cid, model = databricks(c, admin), drafting_model(c, admin)
    made = created(c, admin, body(cid, model))
    recorded = FakeProvider.generate

    def crash(*args: Any) -> Any:
        recorded(*args)  # the call reaches the provider (and is recorded)
        raise _Crash

    monkeypatch.setattr(FakeProvider, "generate", crash)
    with pytest.raises(_Crash):
        run_job(c, owner_session, made["id"])
    assert _reason(c, admin, made["id"]) == ("drafting", None)
    assert _holds(owner_session) == [("held", True, True)]
    # the lease expires and the reaper queues the job again
    owner_session.execute(
        text("UPDATE shifde.jobs SET status = 'queued', lease_owner = NULL WHERE id = :j"),
        {"j": job_of(owner_session, made["id"])},
    )
    owner_session.commit()
    assert run_job(c, owner_session, made["id"])
    assert _reason(c, admin, made["id"]) == ("failed", "model_call_unknown")
    assert len(calls.messages) == 1
    # the invocation-hold sweep settles the orphaned hold once its TTL has passed
    owner_session.execute(
        text("UPDATE shifde.budget_reservations SET expires_at = now() - interval '1 minute'")
    )
    owner_session.commit()
    with app_session(app_db, tenant) as s:
        assert reservations.settle_orphaned_invocations(s, tenant) == 1
        s.commit()
    assert _holds(owner_session) == [("settled_unknown", True, True)]


def test_a_lost_permission_or_a_retired_connection_fails_the_job(
    client: TestClient,
    admin: Hdr,
    world: World,
    owner_session: Session,
    calls: Calls,
) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    email = "fde@demo-a.local"
    _, role = limited_user(c, owner_session, admin, world.passwords, email, PERMISSIONS)
    fde = user_session(client, world.passwords, email)
    admin = user_session(client, world.passwords, ADMIN)  # `limited_user` signed the admin out
    c = admin.client
    revoked = created(fde.client, fde, body(cid, model))
    owner_session.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
    owner_session.execute(
        text(
            "DELETE FROM shifde.role_permissions WHERE role_id = :r "
            "AND permission_key = 'appgen:request:write'"
        ),
        {"r": role},
    )
    owner_session.commit()
    assert run_job(c, owner_session, revoked["id"])
    assert _reason(c, admin, revoked["id"]) == ("failed", "access_revoked")

    retired = created(c, admin, body(cid, model))
    row = c.get(f"{A}/connections/{cid}", headers=admin).json()
    done = c.post(f"{A}/connections/{cid}/status", headers=admin,
                  json={"status": "retired", "row_version": row["row_version"]})  # fmt: skip
    assert done.status_code == 200, done.text
    assert run_job(c, owner_session, retired["id"])
    assert _reason(c, admin, retired["id"]) == ("failed", "connection_unavailable")
    assert calls.messages == []


def test_the_prompt_holds_column_names_and_no_row_value(
    admin: Hdr, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    drafted(c, admin, owner_session, body(cid, model, tables=["main.default.customers"]))
    [[(system_role, system), (user_role, user)]] = calls.messages
    assert (system_role, user_role) == ("system", "user")
    assert '"name": "region"' in user and '"name": "customers"' not in user
    assert "main.default.customers" in user and "orders" not in user
    for value in SAMPLE_VALUES:
        assert value not in system and value not in user, value


def test_a_worker_with_the_composition_roots_handlers_runs_the_job(
    admin: Hdr, owner_session: Session, settings: Settings
) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    made = created(c, admin, body(cid, model))
    app: Any = c.app  # Any: the FastAPI app behind the test client
    worker = Worker(settings, worker_id=f"appgen-{uuid7().hex[-6:]}", concurrency=1,
                    lease_seconds=5, poll_ms=50, reap_seconds=3600.0,
                    runtime=app.state.integration,
                    job_handlers=worker_main.job_handlers())  # fmt: skip
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        end = time.monotonic() + 30
        while get(c, admin, made["id"])["status"] == "queued" and time.monotonic() < end:
            time.sleep(0.1)
        end = time.monotonic() + 30
        while get(c, admin, made["id"])["status"] == "drafting" and time.monotonic() < end:
            time.sleep(0.1)
    finally:
        worker.request_stop()
        thread.join(60)
    out = get(c, admin, made["id"])
    assert out["status"] == "drafted", out
    assert out["revisions"][0]["draft"] == SAMPLE_DRAFT
    assert [j[1] for j in draft_jobs(owner_session)] == ["done"]


def test_a_job_of_another_client_finds_nothing(admin: Hdr, owner_session: Session) -> None:
    c = admin.client
    cid, model = databricks(c, admin), drafting_model(c, admin)
    made = created(c, admin, body(cid, model))
    app: Any = c.app  # Any: the FastAPI app behind the test client
    job = job_of(owner_session, made["id"])
    b = UUID(str(tenant_id(owner_session, "demo-b")))
    assert appgen.run_draft_job(app.state.db, app.state.integration, b, job) == "not_found"
    assert get(c, admin, made["id"])["status"] == "queued"
