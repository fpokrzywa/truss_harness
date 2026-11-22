"""Second-person approval of sealed plans (design section 4, section 9 items 1, 11 and 12;
B-2675..B-2679): approve quotes the sealed digest, the requester is refused at the route and by
the `delivery_approvals` trigger, an expired plan stays `expired`, a plan is decided once, reject
is final, a `developer` cannot decide, and every request and decision is audited.

Related principals (B-2733): a service account the requester owns and the owner of a requesting
service account are refused as the requester is, each such refusal is audited `denied`, and the
permission is checked before the settle commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.authz.service import new_pdp
from shifde.core.context import Principal, RequestContext
from shifde.core.db import Database, set_principal, set_tenant
from shifde.core.errors import AppError
from shifde.core.ids import uuid7
from shifde.delivery import schemas
from shifde.delivery.plans import PlanService
from shifde.tenancy.scope import TenantScope
from tests.connections.routes import as_user, system_role_user
from tests.delivery.conftest import ExecutorSide
from tests.delivery.releases import release_for_target
from tests.delivery.routes import (
    address,
    lease_plan,
    plan_result,
    plan_row,
    plans,
    register,
    request_plan,
    target_body,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws_slug


@dataclass(frozen=True)
class Sealed:
    plan: dict[str, Any]  # Any: the PlanOut body after sealing
    requester: str  # A's owner
    approver: str  # another tenant administrator of A


def _user_id(owner_db: Database, email: str) -> UUID:
    with owner_db.session() as o:
        uid: UUID = o.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
    return uid


def _request(client: TestClient, world: World, email: str, owner_db: Database) -> dict[str, Any]:
    h = as_user(client, email)
    target = register(client, h, world.a.slug, target_body(ws_slug(world.a), address()))
    release = release_for_target(owner_db, target["id"]).id
    made: dict[str, Any] = request_plan(client, h, world.a.slug, target["id"], release)
    client.cookies.clear()
    return made


def _seal(client: TestClient, executor: ExecutorSide) -> None:
    leased = lease_plan(client, executor.token)
    r = plan_result(client, executor.token, leased)
    assert r.status_code == 200, r.text


@pytest.fixture
def sealed(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> Sealed:
    made = _request(client, world, people.owner_a, owner_db)
    _seal(client, executor_a)
    h = as_user(client, people.owner_a)
    plan = client.get(f"{plans(world.a.slug)}/{made['id']}", headers=h).json()
    client.cookies.clear()
    assert plan["status"] == "sealed" and plan["digest"]
    approver = system_role_user(owner_db, world.a.tenant, "tenant_administrator")
    return Sealed(plan, people.owner_a, approver)


def _decide(
    client: TestClient,
    world: World,
    email: str,
    plan_id: object,
    action: str,
    body: dict[str, Any] | None = None,
) -> Any:  # Any: an httpx response
    h = as_user(client, email)
    return client.post(f"{plans(world.a.slug)}/{plan_id}/{action}", json=body or {}, headers=h)


def _code(r: Any, status: int, code: str) -> None:  # Any: an httpx response
    assert r.status_code == status and r.json()["code"] == code, r.text


def _approvals(owner_db: Database, plan_id: object) -> list[Any]:
    with owner_db.session() as o:
        return list(
            o.execute(
                text("SELECT * FROM shifde.delivery_approvals WHERE plan_id = :p"),
                {"p": plan_id},
            )
            .mappings()
            .all()
        )


# ------------------------------------------------------------------ approve
def test_a_second_person_approves_the_sealed_digest(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    r = _decide(
        client,
        world,
        sealed.approver,
        plan["id"],
        "approve",
        {"plan_digest": plan["digest"], "note": "looks right"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    approver_id = _user_id(owner_db, sealed.approver)
    assert out["status"] == "approved" and out["digest"] == plan["digest"]
    assert out["approval"]["decision"] == "approve"
    assert out["approval"]["decided_by"] == str(approver_id)
    assert out["approval"]["note"] == "looks right"
    rows = _approvals(owner_db, plan["id"])
    assert len(rows) == 1
    assert rows[0]["plan_digest"] == plan["digest"] and rows[0]["decided_by"] == approver_id
    assert rows[0]["decision"] == "approve" and rows[0]["tenant_id"] == world.a.tenant
    assert plan_row(owner_db, plan["id"])["status"] == "approved"


def test_the_requester_cannot_decide_their_own_plan(
    client: TestClient, world: World, owner_db: Database, app_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    for action, body in (("approve", {"plan_digest": plan["digest"]}), ("reject", {})):
        r = _decide(client, world, sealed.requester, plan["id"], action, body)
        _code(r, 403, "self_approval_forbidden")
    assert _approvals(owner_db, plan["id"]) == []
    # the trigger is the backstop: a direct insert by the requester is refused
    requester = _user_id(owner_db, sealed.requester)
    for decision in ("approve", "reject"):
        with app_session(app_db, world.a.tenant) as s, pytest.raises(DBAPIError) as exc:
            s.execute(
                text(
                    "INSERT INTO shifde.delivery_approvals (tenant_id, id, plan_id, plan_digest, "
                    "decided_by, decision) VALUES (:t, :i, :p, :d, :u, :x)"
                ),
                {
                    "t": world.a.tenant,
                    "i": uuid7(),
                    "p": plan["id"],
                    "d": plan["digest"],
                    "u": requester,
                    "x": decision,
                },
            )
        assert getattr(exc.value.orig, "sqlstate", None) == "42501"
    assert plan_row(owner_db, plan["id"])["status"] == "sealed"
    # a different person with delivery:plan:approve succeeds
    r = _decide(
        client, world, sealed.approver, plan["id"], "approve", {"plan_digest": plan["digest"]}
    )
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text


def test_the_trigger_refuses_a_wrong_digest_or_an_unsealed_plan(
    client: TestClient, world: World, people: People, owner_db: Database, app_db: Database
) -> None:
    made = _request(client, world, people.owner_a, owner_db)
    other = uuid7()
    for digest in (made["artifact_digest"], "e" * 64):
        with app_session(app_db, world.a.tenant) as s, pytest.raises(DBAPIError) as exc:
            s.execute(
                text(
                    "INSERT INTO shifde.delivery_approvals (tenant_id, id, plan_id, plan_digest, "
                    "decided_by, decision) VALUES (:t, :i, :p, :d, :u, 'approve')"
                ),
                {"t": world.a.tenant, "i": uuid7(), "p": made["id"], "d": digest, "u": other},
            )
        assert getattr(exc.value.orig, "sqlstate", None) == "23514"  # check_violation
    assert _approvals(owner_db, made["id"]) == []


def test_approve_must_quote_the_sealed_digest(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    r = _decide(client, world, sealed.approver, plan["id"], "approve", {"plan_digest": "e" * 64})
    _code(r, 409, "plan_digest_mismatch")
    r = _decide(client, world, sealed.approver, plan["id"], "approve", {"plan_digest": "nope"})
    assert r.status_code == 422, r.text
    r = _decide(client, world, sealed.approver, plan["id"], "approve", {})
    assert r.status_code == 422, r.text
    assert _approvals(owner_db, plan["id"]) == []
    assert plan_row(owner_db, plan["id"])["status"] == "sealed"


def test_an_unsealed_plan_cannot_be_decided(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    made = _request(client, world, people.owner_a, owner_db)
    approver = system_role_user(owner_db, world.a.tenant, "tenant_administrator")
    r = _decide(client, world, approver, made["id"], "approve", {"plan_digest": "a" * 64})
    _code(r, 409, "plan_not_sealed")
    _code(_decide(client, world, approver, made["id"], "reject"), 409, "plan_not_sealed")
    assert _approvals(owner_db, made["id"]) == []


# ------------------------------------------------------------------ expiry
def _expired_plan(owner_db: Database, world: World, template: dict[str, Any]) -> UUID:
    """A sealed plan whose 15 minutes ended, inserted as the schema owner (the seal guard is
    BEFORE UPDATE only, so a sealed plan cannot be moved back in time)."""
    plan_id = uuid7()
    with owner_db.session() as o:
        o.execute(
            text(
                "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, target_revision, "
                "artifact_id, artifact_digest, application_key, display_name, status, "
                "requested_by, steps, snapshot, adapter_version, toolchain_digest, digest, "
                "sealed_at, expires_at) SELECT tenant_id, :id, target_id, target_revision, "
                "artifact_id, artifact_digest, application_key, display_name, 'sealed', "
                "requested_by, steps, snapshot, adapter_version, toolchain_digest, digest, "
                "now() - interval '20 minutes', now() - interval '5 minutes' "
                "FROM shifde.delivery_plans WHERE id = :p"
            ),
            {"id": plan_id, "p": template["id"]},
        )
        o.commit()
    return plan_id


def test_an_expired_plan_cannot_be_approved_and_reads_expired(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan_id = _expired_plan(owner_db, world, sealed.plan)
    body = {"plan_digest": sealed.plan["digest"]}
    r = _decide(client, world, sealed.approver, plan_id, "approve", body)
    _code(r, 409, "plan_expired")
    row = plan_row(owner_db, plan_id)
    assert row["status"] == "expired" and row["digest"] == sealed.plan["digest"]
    assert _approvals(owner_db, plan_id) == []
    _code(_decide(client, world, sealed.approver, plan_id, "approve", body), 409, "plan_expired")
    _code(_decide(client, world, sealed.approver, plan_id, "reject"), 409, "plan_expired")


def test_reading_settles_an_expired_plan(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan_id = _expired_plan(owner_db, world, sealed.plan)
    h = as_user(client, sealed.approver)
    r = client.get(f"{plans(world.a.slug)}/{plan_id}", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "expired", r.text
    assert plan_row(owner_db, sealed.plan["id"])["status"] == "sealed"  # still in its window


# ------------------------------------------------------------------ decided once
def test_a_plan_is_decided_once(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    body = {"plan_digest": plan["digest"]}
    assert _decide(client, world, sealed.approver, plan["id"], "approve", body).status_code == 200
    _code(
        _decide(client, world, sealed.approver, plan["id"], "approve", body),
        409,
        "plan_already_decided",
    )
    _code(
        _decide(client, world, sealed.approver, plan["id"], "reject"), 409, "plan_already_decided"
    )
    assert len(_approvals(owner_db, plan["id"])) == 1


def test_a_concurrent_decision_is_plan_already_decided(
    client: TestClient, world: World, owner_db: Database, app_db: Database, sealed: Sealed
) -> None:
    """Another decision lands first while the plan still reads `sealed`: the unique key refuses
    the route's insert, and the constraint's name is not passed on."""
    plan = sealed.plan
    with app_session(app_db, world.a.tenant) as s:
        s.execute(
            text(
                "INSERT INTO shifde.delivery_approvals (tenant_id, id, plan_id, plan_digest, "
                "decided_by, decision) VALUES (:t, :i, :p, :d, :u, 'reject')"
            ),
            {
                "t": world.a.tenant,
                "i": uuid7(),
                "p": plan["id"],
                "d": plan["digest"],
                "u": uuid7(),
            },
        )
        s.commit()
    r = _decide(
        client, world, sealed.approver, plan["id"], "approve", {"plan_digest": plan["digest"]}
    )
    _code(r, 409, "plan_already_decided")
    assert "uq_delivery_approvals" not in r.text
    assert plan_row(owner_db, plan["id"])["status"] == "sealed"
    assert len(_approvals(owner_db, plan["id"])) == 1


def test_reject_is_final(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    r = _decide(client, world, sealed.approver, plan["id"], "reject", {"note": "not now"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "rejected" and out["approval"]["decision"] == "reject"
    assert out["approval"]["note"] == "not now"
    rows = _approvals(owner_db, plan["id"])
    assert len(rows) == 1 and rows[0]["plan_digest"] == plan["digest"]
    body = {"plan_digest": plan["digest"]}
    _code(
        _decide(client, world, sealed.approver, plan["id"], "approve", body),
        409,
        "plan_already_decided",
    )
    assert plan_row(owner_db, plan["id"])["status"] == "rejected"


def test_a_note_is_limited_to_2000_characters(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    r = _decide(client, world, sealed.approver, sealed.plan["id"], "reject", {"note": "x" * 2001})
    assert r.status_code == 422, r.text
    assert _approvals(owner_db, sealed.plan["id"]) == []


# ------------------------------------------------------------------ permissions and audit
def test_a_developer_cannot_decide(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    developer = system_role_user(owner_db, world.a.tenant, "developer")
    plan = sealed.plan
    r = _decide(client, world, developer, plan["id"], "approve", {"plan_digest": plan["digest"]})
    assert r.status_code == 403, r.text
    assert _decide(client, world, developer, plan["id"], "reject").status_code == 403
    # a developer reads the plan (delivery:plan:read)
    h = as_user(client, developer)
    assert client.get(f"{plans(world.a.slug)}/{plan['id']}", headers=h).status_code == 200
    assert _approvals(owner_db, plan["id"]) == []


def test_request_approve_and_reject_are_audited(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
    sealed: Sealed,
) -> None:
    second = _request(client, world, people.owner_a, owner_db)
    _seal(client, executor_a)
    body = {"plan_digest": sealed.plan["digest"]}
    assert _decide(client, world, sealed.approver, sealed.plan["id"], "approve", body).is_success
    assert _decide(client, world, sealed.approver, second["id"], "reject").is_success
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT action, target_type, target_id::text AS target_id, actor_id, workspace_id, details "
                "FROM shifde.audit_events WHERE tenant_id = :t AND target_type = 'delivery_plan' "
                "ORDER BY chain_seq"
            ),
            {"t": world.a.tenant},
        ).all()
    assert [(r.action, r.target_id) for r in rows] == [
        ("delivery.plan.requested", sealed.plan["id"]),
        ("delivery.plan.requested", second["id"]),
        ("delivery.plan.approved", sealed.plan["id"]),
        ("delivery.plan.rejected", second["id"]),
    ]
    approver = _user_id(owner_db, sealed.approver)
    for row in rows:
        assert row.workspace_id == world.a.ws
        assert row.details["plan_id"] == row.target_id
        assert "content" not in row.details
    approved, rejected = rows[2], rows[3]
    assert approved.actor_id == approver and rejected.actor_id == approver
    assert approved.details["digest"] == sealed.plan["digest"]
    assert approved.details["target_id"] == sealed.plan["target_id"]
    assert rejected.details["digest"] is not None
    assert rows[0].details["artifact_digest"] == sealed.plan["artifact_digest"]


def test_an_unknown_plan_is_not_found(client: TestClient, world: World, sealed: Sealed) -> None:
    unknown = uuid7()
    body = {"plan_digest": sealed.plan["digest"]}
    for action, b in (("approve", body), ("reject", {})):
        r = _decide(client, world, sealed.approver, unknown, action, b)
        assert r.status_code == 404 and r.json()["detail"] == "plan not found", r.text


# ------------------------------------------------------------------ related principals (B-2733)
def _service_account(owner_db: Database, tenant: UUID, owner: UUID | None) -> UUID:
    """An active service account of `tenant` owned by `owner`, bound to `tenant_administrator`
    at organization scope (so it holds `delivery:plan:approve`)."""
    sa = uuid7()
    with owner_db.session() as o:
        o.execute(
            text(
                "INSERT INTO shifde.service_accounts (id, tenant_id, name, owner_principal_id) "
                "VALUES (:i, :t, :n, :o)"
            ),
            {"i": sa, "t": tenant, "n": f"sa-{sa.hex[-12:]}", "o": owner},
        )
        o.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
        o.execute(
            text(
                "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                "role_id, scope_type, scope_id) SELECT :i, :t, 'service_account', :s, r.id, "
                "'tenant', :t FROM shifde.roles r WHERE r.tenant_id IS NULL "
                "AND r.key = 'tenant_administrator'"
            ),
            {"i": uuid7(), "t": tenant, "s": sa},
        )
        o.commit()
    return sa


def _decide_as_service_account(
    app_db: Database, world: World, sa: UUID, plan_id: object, decision: str, digest: str
) -> Any:  # Any: a PlanOut, or the AppError raised
    """`PlanService` approve or reject in A as the service account `sa` (an API key caller)."""
    with app_db.session() as session:
        set_tenant(session, world.a.tenant)
        set_principal(session, sa)
        principal = Principal(
            user_id=sa,
            email=f"{sa.hex}@sa.test",
            display_name="sa",
            principal_type="service_account",
            credential="api_key",
            tenant_id=world.a.tenant,
        )
        scope = TenantScope(
            session=session,
            ctx=RequestContext(request_id="r-sa", principal=principal, tenant_id=world.a.tenant),
            principal=principal,
            tenant_id=world.a.tenant,
            tenant_slug=world.a.slug,
            tenant_name="A",
            tenant_status="active",
            pdp=new_pdp(session, principal, world.a.tenant),
        )
        service = PlanService(scope)
        pid = UUID(str(plan_id))
        try:
            if decision == "approve":
                return service.approve(pid, schemas.PlanApprove(plan_digest=digest))
            return service.reject(pid, schemas.PlanReject())
        except AppError as exc:
            return exc


def _denials(owner_db: Database, tenant: UUID) -> list[Any]:
    with owner_db.session() as o:
        return list(
            o.execute(
                text(
                    "SELECT action, outcome, target_id::text AS target_id, actor_id, details "
                    "FROM shifde.audit_events WHERE tenant_id = :t "
                    "AND action IN ('delivery.plan.approve', 'delivery.plan.reject') "
                    "ORDER BY chain_seq"
                ),
                {"t": tenant},
            ).all()
        )


def _requested_by(owner_db: Database, template: dict[str, Any], requester: UUID) -> UUID:
    """A copy of the sealed plan `template`, still in its window, requested by `requester`
    (inserted as the schema owner, as `_expired_plan` does)."""
    plan_id = uuid7()
    with owner_db.session() as o:
        o.execute(
            text(
                "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, target_revision, "
                "artifact_id, artifact_digest, application_key, display_name, status, "
                "requested_by, steps, snapshot, adapter_version, toolchain_digest, digest, "
                "sealed_at, expires_at, release_id, release_digest, evaluation_run_id) "
                "SELECT tenant_id, :id, target_id, target_revision, artifact_id, artifact_digest, "
                "application_key, display_name, 'sealed', :r, steps, snapshot, adapter_version, "
                "toolchain_digest, digest, sealed_at, expires_at, release_id, release_digest, "
                "evaluation_run_id FROM shifde.delivery_plans WHERE id = :p"
            ),
            {"id": plan_id, "r": requester, "p": template["id"]},
        )
        o.commit()
    return plan_id


def test_a_service_account_owned_by_the_requester_cannot_decide(
    world: World, owner_db: Database, app_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    requester = _user_id(owner_db, sealed.requester)
    sa = _service_account(owner_db, world.a.tenant, requester)
    for decision in ("approve", "reject"):
        out = _decide_as_service_account(app_db, world, sa, plan["id"], decision, plan["digest"])
        assert isinstance(out, AppError) and out.code == "self_approval_forbidden", out
        assert out.status == 403
    assert _approvals(owner_db, plan["id"]) == []
    assert plan_row(owner_db, plan["id"])["status"] == "sealed"
    rows = _denials(owner_db, world.a.tenant)
    assert [(r.action, r.outcome, r.target_id, r.actor_id) for r in rows] == [
        ("delivery.plan.approve", "denied", plan["id"], sa),
        ("delivery.plan.reject", "denied", plan["id"], sa),
    ]
    for row in rows:
        assert row.details["plan_id"] == plan["id"]
        assert row.details["reason"] == "decider_owned_by_requester"
        assert row.details["target_id"] == plan["target_id"]
        assert row.details["application_key"] == plan["application_key"]
    assert [r.details["decision"] for r in rows] == ["approve", "reject"]


def test_the_owner_of_a_requesting_service_account_cannot_decide(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    owner = _user_id(owner_db, sealed.approver)
    sa = _service_account(owner_db, world.a.tenant, owner)
    plan_id = _requested_by(owner_db, sealed.plan, sa)
    body = {"plan_digest": sealed.plan["digest"]}
    for action, b in (("approve", body), ("reject", {})):
        r = _decide(client, world, sealed.approver, plan_id, action, b)
        _code(r, 403, "self_approval_forbidden")
    assert _approvals(owner_db, plan_id) == []
    assert plan_row(owner_db, plan_id)["status"] == "sealed"
    rows = _denials(owner_db, world.a.tenant)
    assert [(r.action, r.outcome, r.target_id, r.actor_id) for r in rows] == [
        ("delivery.plan.approve", "denied", str(plan_id), owner),
        ("delivery.plan.reject", "denied", str(plan_id), owner),
    ]
    assert {r.details["reason"] for r in rows} == {"requester_owned_by_decider"}
    assert {r.details["plan_id"] for r in rows} == {str(plan_id)}
    # a person unrelated to the service account decides it
    r = _decide(client, world, sealed.requester, plan_id, "approve", body)
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text


@pytest.mark.parametrize("owned", [False, True])
def test_an_unrelated_service_account_can_approve(
    world: World, owner_db: Database, app_db: Database, sealed: Sealed, owned: bool
) -> None:
    """Owner NULL, or a person other than the requester."""
    other = _user_id(owner_db, sealed.approver) if owned else None
    sa = _service_account(owner_db, world.a.tenant, other)
    plan = sealed.plan
    out = _decide_as_service_account(app_db, world, sa, plan["id"], "approve", plan["digest"])
    assert isinstance(out, schemas.PlanOut), out
    assert out.status == "approved" and out.approval is not None and out.approval.decided_by == sa
    assert plan_row(owner_db, plan["id"])["status"] == "approved"
    assert _denials(owner_db, world.a.tenant) == []


def test_a_sibling_service_account_cannot_decide(
    world: World, owner_db: Database, app_db: Database, sealed: Sealed
) -> None:
    """Two service accounts with the same owner (B-2911)."""
    owner = _user_id(owner_db, sealed.approver)
    requester = _service_account(owner_db, world.a.tenant, owner)
    sibling = _service_account(owner_db, world.a.tenant, owner)
    plan_id = _requested_by(owner_db, sealed.plan, requester)
    digest = sealed.plan["digest"]
    for decision in ("approve", "reject"):
        out = _decide_as_service_account(app_db, world, sibling, plan_id, decision, digest)
        assert isinstance(out, AppError) and out.code == "self_approval_forbidden", out
        assert out.status == 403
    assert _approvals(owner_db, plan_id) == []
    assert plan_row(owner_db, plan_id)["status"] == "sealed"
    rows = _denials(owner_db, world.a.tenant)
    assert [(r.action, r.outcome, r.target_id, r.actor_id) for r in rows] == [
        ("delivery.plan.approve", "denied", str(plan_id), sibling),
        ("delivery.plan.reject", "denied", str(plan_id), sibling),
    ]
    for row in rows:
        assert row.details["plan_id"] == str(plan_id)
        assert row.details["reason"] == "sibling_service_accounts"
        assert row.details["target_id"] == sealed.plan["target_id"]
        assert row.details["application_key"] == sealed.plan["application_key"]
    assert [r.details["decision"] for r in rows] == ["approve", "reject"]


@pytest.mark.parametrize("owned", [False, True])
def test_a_service_account_that_is_no_sibling_can_approve(
    world: World, owner_db: Database, app_db: Database, sealed: Sealed, owned: bool
) -> None:
    """Both owners NULL, or two different owners (B-2911)."""
    first = _user_id(owner_db, sealed.approver) if owned else None
    second = _user_id(owner_db, sealed.requester) if owned else None
    requester = _service_account(owner_db, world.a.tenant, first)
    decider = _service_account(owner_db, world.a.tenant, second)
    plan_id = _requested_by(owner_db, sealed.plan, requester)
    out = _decide_as_service_account(
        app_db, world, decider, plan_id, "approve", sealed.plan["digest"]
    )
    assert isinstance(out, schemas.PlanOut), out
    assert out.status == "approved" and out.approval is not None
    assert out.approval.decided_by == decider
    assert plan_row(owner_db, plan_id)["status"] == "approved"
    assert _denials(owner_db, world.a.tenant) == []


def test_a_refused_self_decision_is_audited_denied(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    requester = _user_id(owner_db, sealed.requester)
    body = {"plan_digest": plan["digest"]}
    r = _decide(client, world, sealed.requester, plan["id"], "approve", body)
    _code(r, 403, "self_approval_forbidden")
    assert len(_denials(owner_db, world.a.tenant)) == 1
    r = _decide(client, world, sealed.requester, plan["id"], "reject")
    _code(r, 403, "self_approval_forbidden")
    rows = _denials(owner_db, world.a.tenant)
    assert [(r.action, r.outcome, r.target_id, r.actor_id) for r in rows] == [
        ("delivery.plan.approve", "denied", plan["id"], requester),
        ("delivery.plan.reject", "denied", plan["id"], requester),
    ]
    for row in rows:
        assert row.details["plan_id"] == plan["id"]
        assert row.details["reason"] == "same_principal"
    assert _approvals(owner_db, plan["id"]) == []
    assert plan_row(owner_db, plan["id"])["status"] == "sealed"


def test_other_refusals_write_no_denial(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan = sealed.plan
    body = {"plan_digest": plan["digest"]}
    developer = system_role_user(owner_db, world.a.tenant, "developer")
    assert _decide(client, world, developer, plan["id"], "approve", body).status_code == 403
    assert _decide(client, world, developer, plan["id"], "reject").status_code == 403
    expired = _expired_plan(owner_db, world, plan)
    _code(_decide(client, world, sealed.approver, expired, "approve", body), 409, "plan_expired")
    _code(_decide(client, world, sealed.approver, expired, "reject"), 409, "plan_expired")
    r = _decide(client, world, sealed.approver, plan["id"], "approve", {"plan_digest": "e" * 64})
    _code(r, 409, "plan_digest_mismatch")
    assert _denials(owner_db, world.a.tenant) == []


def test_the_permission_is_checked_before_the_settle(
    client: TestClient, world: World, owner_db: Database, sealed: Sealed
) -> None:
    plan_id = _expired_plan(owner_db, world, sealed.plan)
    body = {"plan_digest": sealed.plan["digest"]}
    developer = system_role_user(owner_db, world.a.tenant, "developer")
    for action, b in (("approve", body), ("reject", {})):
        assert _decide(client, world, developer, plan_id, action, b).status_code == 403
        assert plan_row(owner_db, plan_id)["status"] == "sealed"  # no settle ran
    _code(_decide(client, world, sealed.approver, plan_id, "approve", body), 409, "plan_expired")
    assert plan_row(owner_db, plan_id)["status"] == "expired"
