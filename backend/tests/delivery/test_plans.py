"""Deployment plans (design sections 3 and 4, section 9 items 5, 9 and 17; B-2675..B-2679): the
template artifact, the plan request and its `plan` job, `requested -> planning` on lease, sealing
by the job's result (digest, 15-minute expiry), every `plan_failed` path, the sealed-plan guard,
and the executor's artifact route. Every route runs as `shifde_app`.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.delivery import plans as plan_service
from shifde.delivery import service
from tests.connections.routes import as_user, system_role_user
from tests.delivery.conftest import ExecutorSide
from tests.delivery.routes import (
    address,
    bearer,
    expire,
    job_url,
    lease,
    lease_plan,
    plan_result,
    plan_row,
    plan_value,
    plans,
    proof,
    register,
    request_plan,
    target_body,
    targets,
)
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws_slug

PLAN_FIELDS = {
    "id",
    "target_id",
    "application_key",
    "display_name",
    "status",
    "status_reason",
    "requested_by",
    "artifact_id",
    "artifact_digest",
    "target_revision",
    "steps",
    "snapshot",
    "adapter_version",
    "toolchain_digest",
    "digest",
    "sealed_at",
    "expires_at",
    "created_at",
    "updated_at",
    "approval",
    "deployment_id",
}


def _user_id(owner_db: Database, email: str) -> str:
    with owner_db.session() as o:
        return str(
            o.execute(
                text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
            ).scalar_one()
        )


def _target(client: TestClient, world: World, people: People) -> dict[str, Any]:
    h = as_user(client, people.owner_a)
    made: dict[str, Any] = register(
        client, h, world.a.slug, target_body(ws_slug(world.a), address())
    )
    return made


def _plan(client: TestClient, world: World, people: People) -> dict[str, Any]:
    """A target of A and a plan on it requested by A's owner (the client is signed out)."""
    target = _target(client, world, people)
    h = as_user(client, people.owner_a)
    made: dict[str, Any] = request_plan(client, h, world.a.slug, target["id"])
    client.cookies.clear()
    return made


def _get(client: TestClient, world: World, email: str, plan_id: object) -> Any:
    h = as_user(client, email)
    r = client.get(f"{plans(world.a.slug)}/{plan_id}", headers=h)
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()


def _plan_jobs(owner_db: Database, plan_id: object) -> list[Any]:
    with owner_db.session() as o:
        return list(
            o.execute(text("SELECT * FROM shifde.delivery_jobs WHERE plan_id = :p"), {"p": plan_id})
            .mappings()
            .all()
        )


# ------------------------------------------------------------------ the template artifact
def test_the_template_artifact_is_stored_with_its_digest_and_size(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    content, digest = plan_service.template_artifact()
    assert digest == hashlib.sha256(content).hexdigest()
    assert 0 < len(content) <= plan_service.MAX_ARTIFACT_BYTES
    made = _plan(client, world, people)
    assert set(made) == PLAN_FIELDS  # never the artifact's content
    with owner_db.session() as o:
        row = (
            o.execute(
                text("SELECT * FROM shifde.delivery_artifacts WHERE id = :a"),
                {"a": made["artifact_id"]},
            )
            .mappings()
            .one()
        )
    stored = bytes(row["content"])
    assert row["digest"] == hashlib.sha256(stored).hexdigest() == made["artifact_digest"]
    assert row["size"] == len(stored) and row["target_kind"] == "databricks.app/v1"
    assert stored == content  # the build is deterministic
    assert row["tenant_id"] == world.a.tenant


def test_an_artifact_over_5_mb_is_refused_before_insert(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(client, world, people)
    monkeypatch.setattr(
        plan_service, "create_archive", lambda files: b"x" * (plan_service.MAX_ARTIFACT_BYTES + 1)
    )
    h = as_user(client, people.owner_a)
    r = client.post(
        plans(world.a.slug),
        json={"target_id": target["id"], "application_key": "big", "display_name": "Big"},
        headers=h,
    )
    assert r.status_code == 422 and r.json()["code"] == "validation_error", r.text
    with owner_db.session() as o:
        for table in ("delivery_artifacts", "delivery_plans", "delivery_jobs"):
            n = o.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :t"),
                {"t": world.a.tenant},
            ).scalar_one()
            assert n == 0, table


def test_the_artifact_table_enforces_size_and_content(world: World, app_db: Database) -> None:
    user = uuid7()
    for size, content in ((6, b"short"), (5_242_881, b"x" * 5_242_881)):
        with app_session(app_db, world.a.tenant) as s, pytest.raises(IntegrityError):
            s.execute(
                text(
                    "INSERT INTO shifde.delivery_artifacts (tenant_id, id, target_kind, digest, "
                    "size, content, created_by) VALUES (:t, :i, 'databricks.app/v1', :d, :n, "
                    ":c, :u)"
                ),
                {
                    "t": world.a.tenant,
                    "i": uuid7(),
                    "d": "a" * 64,
                    "n": size,
                    "c": content,
                    "u": user,
                },
            )


# ------------------------------------------------------------------ request
def test_a_request_creates_the_plan_and_one_queued_plan_job(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    target = _target(client, world, people)
    email = system_role_user(owner_db, world.a.tenant, "developer")
    h = as_user(client, email)
    made = request_plan(client, h, world.a.slug, target["id"], display_name="Sales")
    assert made["status"] == "requested" and made["status_reason"] is None
    assert made["requested_by"] == _user_id(owner_db, email)
    assert made["target_id"] == target["id"] and made["target_revision"] == 1
    assert made["application_key"] == "sales-app" and made["display_name"] == "Sales"
    for key in ("steps", "snapshot", "digest", "sealed_at", "expires_at", "approval"):
        assert made[key] is None, key
    jobs = _plan_jobs(owner_db, made["id"])
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "plan" and job["status"] == "queued"
    assert str(job["target_id"]) == target["id"] and job["max_attempts"] == 4
    # a second request for the same application is another plan (not unique per target)
    again = request_plan(client, h, world.a.slug, target["id"])
    assert again["id"] != made["id"]
    assert client.get(f"{plans(world.a.slug)}/{made['id']}", headers=h).json() == made


def test_the_request_body_is_validated(client: TestClient, world: World, people: People) -> None:
    target = _target(client, world, people)
    h = as_user(client, people.owner_a)
    for overrides in (
        {"application_key": "Sales"},
        {"application_key": "1app"},
        {"application_key": ""},
        {"display_name": ""},
        {"display_name": "x" * 201},
        {"extra": 1},
    ):
        request_plan(client, h, world.a.slug, target["id"], status=422, **overrides)


def test_a_plan_needs_a_readable_active_target_in_an_active_scope(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    target = _target(client, world, people)
    h = as_user(client, people.owner_a)
    unknown = request_plan(client, h, world.a.slug, uuid7(), status=404)
    assert unknown["detail"] == "target not found"
    viewer = as_user(client, people.viewer_a)
    hidden = request_plan(client, viewer, world.a.slug, target["id"], status=404)
    assert {k: v for k, v in hidden.items() if k not in ("instance", "request_id")} == {
        k: v for k, v in unknown.items() if k not in ("instance", "request_id")
    }
    h = as_user(client, people.owner_a)
    r = client.post(f"{targets(world.a.slug)}/{target['id']}/archive", headers=h)
    assert r.status_code == 200, r.text
    archived = request_plan(client, h, world.a.slug, target["id"], status=409)
    assert archived["code"] == "target_unavailable"
    other = _target(client, world, people)
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.workspaces SET status = 'archived' WHERE id = :w"),
            {"w": world.a.ws},
        )
        o.commit()
    h = as_user(client, people.owner_a)
    assert request_plan(client, h, world.a.slug, other["id"], status=409)["code"] == (
        "scope_archived"
    )
    with owner_db.session() as o:
        for table in ("delivery_artifacts", "delivery_plans", "delivery_jobs"):
            n = o.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE tenant_id = :t"),
                {"t": world.a.tenant},
            ).scalar_one()
            assert n == 0, table


def test_list_and_its_filters(client: TestClient, world: World, people: People) -> None:
    first = _plan(client, world, people)
    second = _plan(client, world, people)
    h = as_user(client, people.owner_a)
    listed = client.get(f"{plans(world.a.slug)}?limit=1", headers=h).json()
    assert [p["id"] for p in listed["items"]] == [first["id"]] and listed["next_cursor"]
    rest = client.get(
        f"{plans(world.a.slug)}?limit=1&cursor={listed['next_cursor']}", headers=h
    ).json()
    assert [p["id"] for p in rest["items"]] == [second["id"]]
    by_target = client.get(
        plans(world.a.slug), params={"target": second["target_id"]}, headers=h
    ).json()
    assert [p["id"] for p in by_target["items"]] == [second["id"]]
    sealed = client.get(plans(world.a.slug), params={"status": "sealed"}, headers=h).json()
    assert sealed["items"] == []
    bad = client.get(plans(world.a.slug), params={"status": "bogus"}, headers=h)
    assert bad.status_code == 422
    viewer = as_user(client, people.viewer_a)
    assert client.get(plans(world.a.slug), headers=viewer).status_code == 403


# ------------------------------------------------------------------ the plan job
def test_lease_moves_the_plan_to_planning_and_a_result_seals_it(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    made = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    assert leased["job"]["plan_id"] == made["id"]
    assert leased["job"]["target_id"] == made["target_id"]
    assert plan_row(owner_db, made["id"])["status"] == "planning"
    r = plan_result(client, executor_a.token, leased)
    assert r.status_code == 200, r.text
    row = plan_row(owner_db, made["id"])
    assert row["status"] == "sealed" and row["status_reason"] is None
    assert row["expires_at"] - row["sealed_at"] == timedelta(minutes=15)
    value = plan_value()
    steps = [{**value["steps"][0], "reversible": False}]
    assert row["steps"] == steps and row["adapter_version"] == "0.1.0"
    assert row["toolchain_digest"] == value["toolchain_digest"]
    assert row["snapshot"]["fingerprint"] == value["snapshot"]["fingerprint"]
    assert row["digest"] == service.plan_digest(
        artifact_digest=row["artifact_digest"],
        target_revision=row["target_revision"],
        snapshot_fingerprint=row["snapshot"]["fingerprint"],
        steps=row["steps"],
        adapter_version=row["adapter_version"],
        toolchain_digest=row["toolchain_digest"],
    )
    out = _get(client, world, people.owner_a, made["id"])
    assert out["status"] == "sealed" and out["digest"] == row["digest"]
    assert out["steps"] == steps
    assert out["approval"] is None


def test_a_failed_result_fails_the_plan(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    made = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    r = plan_result(
        client, executor_a.token, leased, status="failed", error_code="execution_failed"
    )
    assert r.status_code == 200, r.text
    row = plan_row(owner_db, made["id"])
    assert row["status"] == "plan_failed" and "execution_failed" in row["status_reason"]
    assert row["digest"] is None and row["sealed_at"] is None


@pytest.mark.parametrize(
    ("steps", "reason"),
    [
        ([], "must contain steps"),
        ([{"key": "drop", "action": "delete", "resource_key": "app"}], "forbids deletion"),
        ([{"key": "what", "action": "unknown", "resource_key": "app"}], "forbids deletion"),
    ],
)
def test_the_default_policy_fails_the_plan(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
    steps: list[dict[str, str]],
    reason: str,
) -> None:
    made = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    r = plan_result(client, executor_a.token, leased, status="succeeded", value=plan_value(steps))
    assert r.status_code == 200, r.text
    row = plan_row(owner_db, made["id"])
    assert row["status"] == "plan_failed" and reason in row["status_reason"]
    assert row["digest"] is None


def test_an_archived_or_changed_target_fails_the_plan(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    archived = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    h = as_user(client, people.owner_a)
    r = client.post(f"{targets(world.a.slug)}/{archived['target_id']}/archive", headers=h)
    assert r.status_code == 200, r.text
    client.cookies.clear()
    assert plan_result(client, executor_a.token, leased).status_code == 200
    row = plan_row(owner_db, archived["id"])
    assert row["status"] == "plan_failed" and row["status_reason"] == (
        "the target is no longer active"
    )
    changed = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    with owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.delivery_targets SET revision = revision + 1 WHERE id = :t"),
            {"t": changed["target_id"]},
        )
        o.commit()
    assert plan_result(client, executor_a.token, leased).status_code == 200
    row = plan_row(owner_db, changed["id"])
    assert row["status"] == "plan_failed" and row["status_reason"] == "the target changed"


def test_an_invalid_result_value_is_refused_and_changes_nothing(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    made = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    for value in (
        {},
        {**plan_value(), "toolchain_digest": "nope"},
        {**plan_value(), "adapter_version": ""},
        {**plan_value(), "extra": 1},
        {**plan_value(), "steps": [{"key": "k", "action": "explode", "resource_key": "r"}]},
    ):
        r = plan_result(client, executor_a.token, leased, status="succeeded", value=value)
        assert r.status_code == 422 and r.json()["code"] == "validation_error", (value, r.text)
    assert plan_row(owner_db, made["id"])["status"] == "planning"
    assert plan_result(client, executor_a.token, leased).status_code == 200
    assert plan_row(owner_db, made["id"])["status"] == "sealed"


def test_a_result_for_a_plan_no_longer_planning_changes_nothing(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    made = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.delivery_plans SET status = 'plan_failed', "
                "status_reason = 'stopped' WHERE id = :p"
            ),
            {"p": made["id"]},
        )
        o.commit()
    assert plan_result(client, executor_a.token, leased).status_code == 200
    row = plan_row(owner_db, made["id"])
    assert row["status"] == "plan_failed" and row["status_reason"] == "stopped"
    assert row["digest"] is None and row["steps"] is None


def test_a_lost_plan_job_fails_the_plan(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    """Expired leases re-queue the job three times; the fourth loss makes it `lost`, and the
    next lease call fails the plan."""
    made = _plan(client, world, people)
    job = _plan_jobs(owner_db, made["id"])[0]["id"]
    for _ in range(4):
        lease_plan(client, executor_a.token)
        expire(owner_db, job)
    assert lease(client, executor_a.token, kinds=["plan"]).status_code == 204
    assert _plan_jobs(owner_db, made["id"])[0]["status"] == "lost"
    row = plan_row(owner_db, made["id"])
    assert row["status"] == "plan_failed" and row["status_reason"] == "executor lost"


def test_a_dead_executor_surfaces_on_read(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    """Settled call 9: the plan routes sweep too, so no executor poll is needed."""
    made = _plan(client, world, people)
    job = _plan_jobs(owner_db, made["id"])[0]["id"]
    for _ in range(3):
        lease_plan(client, executor_a.token)
        expire(owner_db, job)
    lease_plan(client, executor_a.token)  # the fourth and last attempt
    expire(owner_db, job)
    assert plan_row(owner_db, made["id"])["status"] == "planning"
    out = _get(client, world, people.owner_a, made["id"])
    assert out["status"] == "plan_failed" and out["status_reason"] == "executor lost"


# ------------------------------------------------------------------ the sealed-plan guard
def _sealed(
    client: TestClient, world: World, people: People, executor: ExecutorSide
) -> dict[str, Any]:
    made = _plan(client, world, people)
    leased = lease_plan(client, executor.token)
    assert plan_result(client, executor.token, leased).status_code == 200
    return made


def _refused(app_db: Database, tenant: UUID, sql: str, **params: Any) -> None:
    with app_session(app_db, tenant) as s, pytest.raises(DBAPIError) as exc:
        s.execute(text(sql), params)
    assert getattr(exc.value.orig, "sqlstate", None) == "42501", sql


def test_a_sealed_plan_is_immutable(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    app_db: Database,
    owner_db: Database,
) -> None:
    made = _sealed(client, world, people, executor_a)
    before = dict(plan_row(owner_db, made["id"]))
    for column, value in (
        ("steps", "CAST('[]' AS jsonb)"),
        ("digest", f"'{'f' * 64}'"),
        ("snapshot", "CAST('{}' AS jsonb)"),
        ("adapter_version", "'9.9.9'"),
        ("toolchain_digest", f"'{'f' * 64}'"),
        ("sealed_at", "sealed_at + interval '1 hour'"),
        ("expires_at", "expires_at + interval '1 hour'"),
    ):
        _refused(
            app_db,
            world.a.tenant,
            f"UPDATE shifde.delivery_plans SET {column} = {value} WHERE id = :p",
            p=made["id"],
        )
    # no edge out of the state machine
    for status in ("requested", "planning", "plan_failed"):
        _refused(
            app_db,
            world.a.tenant,
            "UPDATE shifde.delivery_plans SET status = :s WHERE id = :p",
            s=status,
            p=made["id"],
        )
    # columns the app holds no grant on, and no DELETE
    for sql in (
        "UPDATE shifde.delivery_plans SET requested_by = gen_random_uuid() WHERE id = :p",
        "UPDATE shifde.delivery_plans SET target_revision = 9 WHERE id = :p",
        "DELETE FROM shifde.delivery_plans WHERE id = :p",
    ):
        _refused(app_db, world.a.tenant, sql, p=made["id"])
    assert dict(plan_row(owner_db, made["id"])) == before
    # a requested plan cannot skip to approved, and a final status is final
    other = _plan(client, world, people)
    _refused(
        app_db,
        world.a.tenant,
        "UPDATE shifde.delivery_plans SET status = 'approved' WHERE id = :p",
        p=other["id"],
    )
    with app_session(app_db, world.a.tenant) as s:
        s.execute(
            text("UPDATE shifde.delivery_plans SET status = 'plan_failed' WHERE id = :p"),
            {"p": other["id"]},
        )
        s.commit()
    _refused(
        app_db,
        world.a.tenant,
        "UPDATE shifde.delivery_plans SET status = 'planning' WHERE id = :p",
        p=other["id"],
    )


# ------------------------------------------------------------------ the executor's artifact
def test_the_artifact_route_serves_the_plans_archive(
    client: TestClient,
    world: World,
    people: People,
    executor_a: ExecutorSide,
    owner_db: Database,
) -> None:
    made = _plan(client, world, people)
    leased = lease_plan(client, executor_a.token)
    url = job_url(leased["job"]["id"], "artifact")
    r = client.get(url, params=proof(leased), headers=bearer(executor_a.token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["x-artifact-digest"] == made["artifact_digest"]
    assert hashlib.sha256(r.content).hexdigest() == made["artifact_digest"]
    content, _ = plan_service.template_artifact()
    assert r.content == content
    stale = {"lease_id": str(uuid7()), "fence": leased["fence"]}
    r = client.get(url, params=stale, headers=bearer(executor_a.token))
    assert r.status_code == 409 and r.json()["code"] == "lease_lost", r.text
