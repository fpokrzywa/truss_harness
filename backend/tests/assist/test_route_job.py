"""SP7-3 (design 5.3, 9.4, 9.5 and section 10): the `assist.route` job, the model fallback.

The job routes an undecided request by `model` through `ROUTER_SCRIPT` and records one
`model_call` usage row in the request's project by the requester; the provider sees only the
request text and the candidates. A null answer, a key outside the candidates and a reply that is
not the closed JSON object are `route_unclear` (the last with `invalid_model_output` in the audit
row). A job that finds its request `classifying` (a crash after the commit) records
`model_call_unknown` without a second provider call, and the hold sweep settles the hold
`settled_unknown`. A lost `assist:request:write` is `access_revoked`, a missing router model
`router_model_missing`, a requester without `model:invoke` fails with the PDP code, and the SHI
provider off for B fails B's routing with the `models_admin` code while A's routes. The job runs
only under its own organization, and the composition root registers it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any  # Any: JSON bodies and row values
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import worker_main
from shifde.adapters.model_providers import FakeProvider
from shifde.assist import service as assist
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.ledger import reservations
from shifde.models_admin.repository import SHI_PROVIDER_ID
from tests._support.assist_fixtures import (
    PICKS_COST,
    PICKS_DEPLOYMENT,
    PICKS_INVALID,
    PICKS_NULL,
    ROUTER_SCRIPT,
    SPECIALIST_SCRIPT,
    assist_model,
    audit_of,
    read,
    request_row,
    route_jobs,
    run_route_job,
    sent,
    set_status,
)
from tests._support.fullapp import World
from tests._support.p2util import tenant_id
from tests._support.p3util import ADMIN, ADMIN_B, A, B
from tests._support.p5util import Hdr, limited_user, user_session
from tests.appgen.helpers import Calls, record_calls
from tests.assist.helpers import enable, installed, rows, set_settings
from tests.conftest import CLEAN_TABLES
from tests.ledger.util import add_budget
from tests.tenancy.conftest import app_session

for _table in ("budgets", "budget_reservations", "usage_events"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)

PERMISSIONS = [
    "workspace:read",
    "project:read",
    "model:read",
    "model:invoke",
    "assist:request:read",
    "assist:request:write",
]
PLATFORM_ADMIN = "platform@shifde.local"


@pytest.fixture
def admin(client: TestClient, world: World) -> Hdr:
    h = user_session(client, world.passwords, ADMIN)
    enable(h)
    return h


@pytest.fixture
def model_id(admin: Hdr, owner_session: Session) -> str:
    model = assist_model(admin.client, admin)
    set_settings(owner_session, "demo-a", model_routing_enabled=True, router_model_id=model)
    return model


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    return record_calls(monkeypatch)


def _reason(c: TestClient, h: Hdr, request_id: str, org: str = A) -> tuple[str, str | None]:
    out = read(c, h, request_id, org)
    return out["status"], out["status_reason"]


def _holds(owner: Session) -> list[tuple[Any, ...]]:
    return rows(
        owner,
        "SELECT status, invocation_id IS NOT NULL, provider_request_at IS NOT NULL "
        "FROM shifde.budget_reservations ORDER BY id",
    )


def _member(
    client: TestClient, world: World, admin: Hdr, owner: Session, email: str, perms: list[str]
) -> tuple[Hdr, str]:
    _, role = limited_user(admin.client, owner, admin, world.passwords, email, perms)
    return user_session(client, world.passwords, email), role


def test_the_job_routes_by_model_and_records_one_model_call(
    admin: Hdr, model_id: str, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    add_budget(owner_session, tenant_id(owner_session, "demo-a"), amount="1000000")
    owner_session.commit()
    [cost] = installed(admin, model_id, keys=["cost"])
    solution = c.post(f"{A}/workspaces/main/projects/sample/solutions", headers=admin,
                      json={"name": "ctx", "target_kind": "databricks.app/v1"}).json()  # fmt: skip
    other = sent(c, admin, {"text": "another request about the north region"}, 202)
    text_in = f"evaluate the cost of this agent {PICKS_COST}"
    out = sent(
        c, admin, {"text": text_in, "context": {"kind": "solution", "id": solution["id"]}}, 202
    )
    assert out["candidates"] == ["cost", "evaluation"]
    assert run_route_job(c, owner_session, out["id"])
    done = read(c, admin, out["id"])
    assert (done["status"], done["route_method"], done["specialist_key"]) == (
        "routed", "model", "cost",
    )  # fmt: skip
    assert done["rule_id"] is None and done["run"] is not None
    run = c.get(f"{A}/runs/{done['run_id']}", headers=admin).json()
    assert run["agent_id"] == cost["agent_id"]
    usage = rows(owner_session, "SELECT event_type, model_id::text, project_id::text, "
                 "principal_id::text FROM shifde.usage_events")  # fmt: skip
    assert usage == [("model_call", model_id, out["project_id"], out["requested_by"])]
    assert _holds(owner_session) == [("settled", True, True)]
    [(_, status, _)] = route_jobs(owner_session, out["id"])
    assert status == "done"
    # the router saw the text and the candidates, nothing else
    [[(system_role, system), (user_role, user)]] = calls.messages
    assert (system_role, user_role) == ("system", "user")
    assert json.loads(user) == {"text": text_in, "candidates": ["cost", "evaluation"]}
    assert user == json.dumps({"text": text_in, "candidates": ["cost", "evaluation"]})
    for absent in (solution["id"], "north region", other["id"], "main.default"):
        assert absent not in user and absent not in system
    assert '"context"' not in user and "solution" not in user
    for key in ("research", "solution_design", "build", "evaluation", "cost", "deployment"):
        assert f"- {key}: " in system


@pytest.mark.parametrize(
    ("marker", "detail"),
    [(PICKS_NULL, None), (PICKS_DEPLOYMENT, None), (PICKS_INVALID, "invalid_model_output")],
)
def test_an_unusable_answer_is_route_unclear(
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    calls: Calls,
    marker: str,
    detail: str | None,
) -> None:
    c = admin.client
    out = sent(c, admin, {"text": f"evaluate the cost of this agent {marker}"}, 202)
    assert run_route_job(c, owner_session, out["id"])
    assert _reason(c, admin, out["id"]) == ("refused", "route_unclear")
    assert len(calls.messages) == 1
    events = audit_of(owner_session, out["id"])
    assert [a for a, _ in events] == ["assist.request.created", "assist.request.classifying",
                                      "assist.request.refused"]  # fmt: skip
    assert events[-1][1].get("detail") == detail


class _Crash(BaseException):
    """The worker process dies in the provider call (nothing after it runs)."""


def test_a_crash_after_classifying_records_model_call_unknown_once(
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    app_db: Database,
    calls: Calls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = admin.client
    tenant = tenant_id(owner_session, "demo-a")
    add_budget(owner_session, tenant, amount="1000000")
    owner_session.commit()
    out = sent(c, admin, {"text": f"tell me a joke {PICKS_COST}"}, 202)
    recorded = FakeProvider.generate

    def crash(*args: Any) -> Any:
        recorded(*args)  # the call reaches the provider (and is recorded)
        raise _Crash

    monkeypatch.setattr(FakeProvider, "generate", crash)
    with pytest.raises(_Crash):
        run_route_job(c, owner_session, out["id"])
    assert _reason(c, admin, out["id"]) == ("classifying", None)
    assert _holds(owner_session) == [("held", True, True)]
    # the lease expires and the reaper queues the job again
    owner_session.rollback()
    owner_session.execute(
        text("UPDATE shifde.jobs SET status = 'queued', lease_owner = NULL WHERE id = :j"),
        {"j": request_row(owner_session, out["id"])["job_id"]},
    )
    owner_session.commit()
    assert run_route_job(c, owner_session, out["id"])
    assert _reason(c, admin, out["id"]) == ("failed", "model_call_unknown")
    assert len(calls.messages) == 1
    owner_session.execute(
        text("UPDATE shifde.budget_reservations SET expires_at = now() - interval '1 minute'")
    )
    owner_session.commit()
    with app_session(app_db, tenant) as s:
        assert reservations.settle_orphaned_invocations(s, tenant) == 1
        s.commit()
    assert _holds(owner_session) == [("settled_unknown", True, True)]


def test_a_lost_write_permission_is_access_revoked(
    client: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    calls: Calls,
) -> None:
    member, role = _member(client, world, admin, owner_session, "router@demo-a.local", PERMISSIONS)
    out = sent(member.client, member, {"text": f"tell me a joke {PICKS_COST}"}, 202)
    owner_session.rollback()
    owner_session.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
    owner_session.execute(
        text("DELETE FROM shifde.role_permissions WHERE role_id = :r "
             "AND permission_key = 'assist:request:write'"),
        {"r": role},
    )  # fmt: skip
    owner_session.commit()
    assert run_route_job(client, owner_session, out["id"])
    assert _reason(member.client, member, out["id"]) == ("failed", "access_revoked")
    assert calls.messages == []


def test_a_missing_router_model_is_router_model_missing(
    admin: Hdr, model_id: str, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    out = sent(c, admin, {"text": "tell me a joke"}, 202)
    set_settings(owner_session, "demo-a", router_model_id=None)
    assert run_route_job(c, owner_session, out["id"])
    assert _reason(c, admin, out["id"]) == ("failed", "router_model_missing")
    assert calls.messages == []


def test_a_requester_without_model_invoke_fails_with_the_pdp_code(
    client: TestClient,
    world: World,
    admin: Hdr,
    model_id: str,
    owner_session: Session,
    calls: Calls,
) -> None:
    perms = [p for p in PERMISSIONS if p != "model:invoke"]
    member, _ = _member(client, world, admin, owner_session, "no-invoke@demo-a.local", perms)
    out = sent(member.client, member, {"text": f"tell me a joke {PICKS_COST}"}, 202)
    assert run_route_job(client, owner_session, out["id"])
    assert _reason(member.client, member, out["id"]) == ("failed", "forbidden")
    assert calls.messages == []


def test_the_shi_switch_off_for_b_fails_bs_routing_while_as_routes(
    client: TestClient, world: World, admin: Hdr, owner_session: Session, calls: Calls
) -> None:
    app: Any = client.app  # Any: the FastAPI app behind the test client
    integration = app.state.integration
    app.state.integration = dataclasses.replace(
        integration,
        settings=integration.settings.model_copy(
            update={"shi_provider_config": {"script": ROUTER_SCRIPT + SPECIALIST_SCRIPT}}
        ),
    )
    shared = uuid7()
    owner_session.rollback()
    owner_session.execute(
        text("INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, settings, "
             "status) VALUES (:p, NULL, 'SHI', 'fake', '{}'::jsonb, 'active') "
             "ON CONFLICT (id) DO UPDATE SET status = 'active'"),
        {"p": SHI_PROVIDER_ID},
    )  # fmt: skip
    owner_session.execute(
        text("INSERT INTO shifde.models (id, tenant_id, provider_id, model_key, display_name, "
             "capabilities, lifecycle) VALUES (:m, NULL, :p, 'shi-chat', 'SHI chat', "
             "ARRAY['chat'], 'active')"),
        {"m": shared, "p": SHI_PROVIDER_ID},
    )  # fmt: skip
    owner_session.commit()
    platform = user_session(client, world.passwords, PLATFORM_ADMIN)
    r = platform.client.put("/api/v1/platform/orgs/demo-a/shi-provider-access",
                            headers=platform, json={"enabled": True})  # fmt: skip
    assert r.status_code == 200, r.text
    b = user_session(client, world.passwords, ADMIN_B)
    enable(b, B)
    for slug in ("demo-a", "demo-b"):
        set_settings(owner_session, slug, model_routing_enabled=True, router_model_id=shared)
    a_out = sent(admin.client, admin, {"text": f"tell me a joke {PICKS_NULL}"}, 202)
    b_out = sent(b.client, b, {"text": f"tell me a joke {PICKS_NULL}"}, 202, org=B)
    assert run_route_job(client, owner_session, b_out["id"], "demo-b")
    assert _reason(b.client, b, b_out["id"], B) == ("failed", "not_found")
    assert calls.messages == []
    assert run_route_job(client, owner_session, a_out["id"])
    # A's router model answered (null: no specialist), so A's routing went through
    assert _reason(admin.client, admin, a_out["id"]) == ("refused", "route_unclear")
    assert len(calls.messages) == 1


def test_a_job_runs_only_under_its_own_organization(
    client: TestClient, world: World, owner_session: Session
) -> None:
    b = user_session(client, world.passwords, ADMIN_B)
    enable(b, B)
    model = assist_model(b.client, b, B)
    set_settings(owner_session, "demo-b", model_routing_enabled=True, router_model_id=model)
    out = sent(b.client, b, {"text": f"tell me a joke {PICKS_COST}"}, 202, org=B)
    job = UUID(str(request_row(owner_session, out["id"])["job_id"]))
    app: Any = client.app  # Any: the FastAPI app behind the test client
    tenant_a = tenant_id(owner_session, "demo-a")
    before = request_row(owner_session, out["id"])
    assert assist.run_route_job(app.state.db, app.state.integration, tenant_a, job) == "not_found"
    assert request_row(owner_session, out["id"]) == before
    assert [j[1] for j in route_jobs(owner_session, out["id"])] == ["queued"]


def test_the_composition_root_registers_the_route_job() -> None:
    handlers = worker_main.job_handlers()
    assert handlers.get(assist.ROUTE_JOB_KIND) is assist.run_route_job


def test_a_queued_request_found_ended_is_left_alone(
    admin: Hdr, model_id: str, owner_session: Session, calls: Calls
) -> None:
    c = admin.client
    out = sent(c, admin, {"text": f"tell me a joke {PICKS_COST}"}, 202)
    set_status(owner_session, out["id"], "refused")
    assert run_route_job(c, owner_session, out["id"])
    assert read(c, admin, out["id"])["status"] == "refused" and calls.messages == []


# ------------------------------------------------------------------ SP7-4: the SP7-3 follow-ups
COST_TEXT = "how much did we spend last week"


def _stuck(
    admin: Hdr, monkeypatch: pytest.MonkeyPatch, owner: Session
) -> dict[str, Any]:  # Any: the request row
    """A request left `handing_over` with no run: the API's run creation crashed."""

    def crash(*args: Any, **kwargs: Any) -> Any:  # Any: RunService.create's signature
        raise RuntimeError("the API process died")

    with monkeypatch.context() as m:
        m.setattr(assist.RunService, "create", crash)
        with pytest.raises(RuntimeError):
            admin.client.post(f"{A}/workspaces/main/projects/sample/assist/requests",
                              headers=admin, json={"text": COST_TEXT})  # fmt: skip
    [(request_id,)] = rows(owner, "SELECT id::text FROM shifde.assist_requests")
    row = request_row(owner, request_id)
    assert (row["status"], row["run_id"]) == ("handing_over", None)
    return row


def test_a_resumed_handover_reruns_the_pre_checks(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(admin, assist_model(admin.client, admin), keys=["cost"])
    row = _stuck(admin, monkeypatch, owner_session)
    toggled = admin.client.post(
        f"{A}/workspaces/main/projects/sample/assist/specialists/cost/disable", headers=admin
    )
    assert toggled.status_code == 200, toggled.text
    assert run_route_job(admin.client, owner_session, str(row["id"]))
    after = request_row(owner_session, str(row["id"]))
    assert (after["status"], after["status_reason"], after["run_id"]) == (
        "failed", "specialist_disabled", None,
    )  # fmt: skip
    assert rows(owner_session, "SELECT count(*) FROM shifde.runs") == [(0,)]
    assert [a for a, _ in audit_of(owner_session, str(row["id"]))][-1] == "assist.request.failed"


def test_a_job_that_is_not_the_requests_job_changes_nothing(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(admin, assist_model(admin.client, admin), keys=["cost"])
    row = _stuck(admin, monkeypatch, owner_session)
    owner_session.execute(
        text("UPDATE shifde.assist_requests SET job_id = :j WHERE id = :r"),
        {"j": uuid7(), "r": row["id"]},
    )
    owner_session.commit()
    before = request_row(owner_session, str(row["id"]))
    app: Any = admin.client.app  # Any: the FastAPI app behind the test client
    tenant = tenant_id(owner_session, "demo-a")
    assert assist.run_route_job(app.state.db, app.state.integration, tenant, row["job_id"]) is None
    assert request_row(owner_session, str(row["id"])) == before
    assert rows(owner_session, "SELECT count(*) FROM shifde.runs") == [(0,)]


def test_an_unknown_request_column_is_a_value_error() -> None:
    from shifde.assist import repository

    with pytest.raises(ValueError, match="bogus"):
        repository.change_request(
            None,  # type: ignore[arg-type]  # refused before any statement
            uuid7(),
            tenant_id=uuid7(),
            expected=["queued"],
            status="failed",
            bogus=1,
        )


def test_the_api_handover_and_the_recovery_job_racing_make_one_run(
    admin: Hdr, owner_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from shifde.runtime.service import RunService

    installed(admin, assist_model(admin.client, admin), keys=["cost"])
    app: Any = admin.client.app  # Any: the FastAPI app behind the test client
    tenant = tenant_id(owner_session, "demo-a")
    both = threading.Barrier(2, timeout=30)
    original = RunService.create
    job: list[threading.Thread] = []
    job_errors: list[BaseException] = []

    def recover(request_id: str) -> None:
        try:
            job_id = request_row(owner_session, request_id)["job_id"]
            assist.run_route_job(app.state.db, app.state.integration, tenant, job_id)
        except BaseException as exc:  # reported by the test
            job_errors.append(exc)

    def racing(self: Any, body: Any, key: Any, **kwargs: Any) -> Any:  # Any: create's signature
        if not job:  # the API's handover: start the recovery job, then race it
            request_id = body.variables["assist_request_id"]
            job.append(threading.Thread(target=recover, args=(request_id,)))
            job[0].start()
        both.wait()
        return original(self, body, key, **kwargs)

    monkeypatch.setattr(RunService, "create", racing)
    out = sent(admin.client, admin, {"text": COST_TEXT})
    job[0].join(30)
    assert job_errors == []
    assert (out["status"], out["status_reason"]) == ("routed", None)
    runs = rows(owner_session, "SELECT id::text FROM shifde.runs WHERE idempotency_key = :k",
                k=f"assist:{out['id']}")  # fmt: skip
    assert runs == [(out["run_id"],)]
    after = request_row(owner_session, out["id"])
    assert (after["status"], str(after["run_id"])) == ("routed", out["run_id"])
    actions = [a for a, _ in audit_of(owner_session, out["id"])]
    assert actions.count("assist.request.routed") == 1
    assert "assist.request.failed" not in actions
