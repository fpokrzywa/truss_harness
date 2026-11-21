"""Second-person approval of sealed plans (design section 4, section 9 items 1, 11 and 12;
B-2675..B-2679): approve quotes the sealed digest, the requester is refused at the route and by
the `delivery_approvals` trigger, an expired plan stays `expired`, a plan is decided once, reject
is final, a `developer` cannot decide, and every request and decision is audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from shifde.core.db import Database
from shifde.core.ids import uuid7
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
