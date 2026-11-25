"""Delivery target, executor and plan routes, client A against client B (plan recipe step 5,
B-1019, B-2664, B-2673, B-2675..B-2679).

Every route of `delivery.api.router` has a test here (`ROUTE_TESTS`, matched against the router):
the caller first reaches their own object, then asks for B's target, executor, credential or plan
under A's organization and gets exactly the answer an id that does not exist gets (404, same body),
and finds B's rows absent from A's lists; B's sealed plan answers A the same 404, also for an
approval quoting its real digest. B's slug in the path gives A the unknown-organization
404. An executor token answers for its own organization's executor only, also when two
organizations' credentials share a prefix. B's job cannot name A's plan: `service.enqueue_job`
refuses it with the unknown-plan 404 (B-2732) ahead of `fk_delivery_jobs__plan` (B-2673), which
still refuses a direct `repository.insert_job`, and B's executor cannot fetch A's plan artifact. B's job and journal step cannot name A's
deployment (`fk_delivery_jobs__deployment`, `fk_delivery_journal__deployment`), B's native
resource row cannot name A's deployment or A's target (`fk_delivery_native_resources__deployment`,
`fk_delivery_native_resources__target`), and B's executor leases none of A's apply jobs. The
settle step is scoped to the caller's organization: B's lost apply, expired plan and held apply
behind an unknown outcome stay byte for byte unchanged through A's lease and A's plan and
deployment list and get, until B's own call settles them. The executor gateway routes (`delivery.gateway_api.router`) have
their own table, `GATEWAY_ROUTE_TESTS`, in `test_gateway_cross_client.py`. Snowflake targets
(SP6-3) answer A the same 404s and stay out of A's list, and B's executor leases none of A's
Snowflake plan jobs. So do Fabric, Palantir and Docker targets (SP8-1), and within one
organization a member bound only to project P1 gets on P2's target of each kind the 404 an
unknown id gets and does not find it in the list. Generated Snowflake apps (SP6-6): in both
directions, a plan on the other organization's target or of its release, its plan (an approval
quoting the real digest included) and its deployment answer the unknown-id 404, its provider is
`model_not_in_target_account`, its rows stay out of the lists and unreadable to `shifde_app`, and
they never change. Generated Docker apps (SP8-9): the same 404s in both directions and for a member
bound only to project P1; a plan on a workspace the other organization claims is
`workspace_not_claimed`, byte for byte an unclaimed or released one's answer apart from the request
id, also at approval; two organizations may bind one project and port on their own daemons; and
B's executor leases none of A's Docker jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: target, executor and plan rows, JSON bodies
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shifde.adapters.native.addresses import address_key
from shifde.core.db import Database
from shifde.core.errors import NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.delivery import api as delivery_api
from shifde.delivery import gateway, repository, service
from shifde.identity.apikeys import hash_key
from tests.board.conftest import add_project, member
from tests.connections.routes import as_user, org, system_role_user
from tests.delivery.app_releases import AppRelease, add_provider
from tests.delivery.conftest import make_executor
from tests.delivery.docker import docker_release, docker_target, rows, workspace, workspace_target
from tests.delivery.releases import release_for_target
from tests.delivery.routes import (
    DOCKER_BINDINGS,
    SP8_KINDS,
    WHOAMI,
    Flow,
    address,
    bearer,
    checkpoint,
    create_executor,
    deployment_jobs,
    deployment_row,
    deployments,
    enqueue,
    executors,
    expire,
    issue,
    job_row,
    job_url,
    lease,
    lease_kind,
    lease_plan,
    make_deployment,
    owner_sql,
    plan_result,
    plan_row,
    plans,
    proof,
    register,
    request_plan,
    snowflake_address,
    snowflake_body,
    sp8_address,
    sp8_body,
    target_body,
    targets,
)
from tests.delivery.snowflake import app_release, app_target, snowflake_plan
from tests.delivery.sp8 import sp8_plan
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, project_slug, ws_slug

_VARYING = {"instance", "request_id"}


def _same_404(r: httpx.Response, missing: httpx.Response) -> None:
    """Refused exactly like an id or slug that does not exist: no existence hint."""
    assert r.status_code == missing.status_code == 404, (r.status_code, r.text)
    body, reference = r.json(), missing.json()
    assert {k: v for k, v in body.items() if k not in _VARYING} == {
        k: v for k, v in reference.items() if k not in _VARYING
    }


@dataclass(frozen=True)
class Side:
    slug: str
    email: str
    target: str


@dataclass(frozen=True)
class Two:
    a: Side
    b: Side


@pytest.fixture
def two(client: TestClient, world: World, people: People) -> Two:
    sides = []
    for c, email in ((world.b, people.owner_b), (world.a, people.owner_a)):
        made = register(client, as_user(client, email), c.slug, target_body(ws_slug(c), address()))
        sides.append(Side(c.slug, email, made["id"]))
    return Two(sides[1], sides[0])


def _b_state(owner_db: Database, two: Two) -> tuple[Any, ...]:
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT name, status, revision, claim_id, updated_at FROM shifde.delivery_targets "
                "WHERE id = :i"
            ),
            {"i": two.b.target},
        ).one()
    return tuple(row)


# ------------------------------------------------------------------ route table
EXECUTORS = "/api/v1/orgs/{org}/delivery/executors"
CREDENTIAL = EXECUTORS + "/{executor_id}/credentials/{credential_id}"
PLANS = "/api/v1/orgs/{org}/delivery/plans"
DEPLOYMENTS = "/api/v1/orgs/{org}/delivery/deployments"
ROUTE_TESTS = {
    ("POST", "/api/v1/orgs/{org}/delivery/targets"): "test_bs_organization_is_unknown_to_a",
    ("GET", "/api/v1/orgs/{org}/delivery/targets"): "test_a_lists_only_its_own_rows",
    ("GET", "/api/v1/orgs/{org}/delivery/targets/{target_id}"): "test_a_cannot_reach_bs_target",
    ("POST", "/api/v1/orgs/{org}/delivery/targets/{target_id}/archive"): (
        "test_a_cannot_reach_bs_target"
    ),
    ("POST", EXECUTORS): "test_bs_executors_are_unknown_to_a",
    ("GET", EXECUTORS): "test_a_lists_only_its_own_executors",
    ("GET", EXECUTORS + "/{executor_id}"): "test_a_cannot_reach_bs_executor",
    ("PATCH", EXECUTORS + "/{executor_id}"): "test_a_cannot_reach_bs_executor",
    ("GET", EXECUTORS + "/{executor_id}/credentials"): "test_a_cannot_reach_bs_executor",
    ("POST", EXECUTORS + "/{executor_id}/credentials"): "test_a_cannot_reach_bs_executor",
    ("POST", CREDENTIAL + "/rotate"): "test_a_cannot_reach_bs_executor",
    ("POST", CREDENTIAL + "/revoke"): "test_a_cannot_reach_bs_executor",
    ("POST", PLANS): "test_a_cannot_plan_on_bs_target",
    ("GET", PLANS): "test_a_lists_only_its_own_plans",
    ("GET", PLANS + "/{plan_id}"): "test_a_cannot_reach_bs_plan",
    ("POST", PLANS + "/{plan_id}/approve"): "test_a_cannot_reach_bs_plan",
    ("POST", PLANS + "/{plan_id}/reject"): "test_a_cannot_reach_bs_plan",
    ("GET", DEPLOYMENTS): "test_a_lists_only_its_own_deployments",
    ("GET", DEPLOYMENTS + "/{deployment_id}"): "test_a_cannot_reach_bs_deployment",
    ("POST", DEPLOYMENTS + "/{deployment_id}/cancel"): "test_a_cannot_reach_bs_deployment",
    ("POST", DEPLOYMENTS + "/{deployment_id}/reconcile"): "test_a_cannot_reach_bs_deployment",
    ("POST", DEPLOYMENTS + "/{deployment_id}/mark-failed"): "test_a_cannot_reach_bs_deployment",
}


def test_every_delivery_route_has_a_cross_client_test() -> None:
    routes = {
        (method, route.path)
        for route in delivery_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert routes == set(ROUTE_TESTS)
    assert all(name in globals() for name in ROUTE_TESTS.values())


def test_bs_organization_is_unknown_to_a(
    client: TestClient, world: World, owner_db: Database, two: Two
) -> None:
    h = as_user(client, two.a.email)
    own = client.post(targets(two.a.slug), headers=h, json=target_body(ws_slug(world.a), address()))
    assert own.status_code == 201, own.text
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    body = target_body(ws_slug(world.b), address())
    before = _b_state(owner_db, two)
    for method, path, payload in (
        ("GET", "/delivery/targets", None),
        ("POST", "/delivery/targets", body),
        ("GET", f"/delivery/targets/{two.b.target}", None),
        ("POST", f"/delivery/targets/{two.b.target}/archive", None),
    ):
        missing = client.request(method, f"{unknown}{path}", headers=h, json=payload)
        _same_404(
            client.request(method, f"{org(two.b.slug)}{path}", headers=h, json=payload), missing
        )
    assert _b_state(owner_db, two) == before


def test_a_cannot_reach_bs_target(client: TestClient, owner_db: Database, two: Two) -> None:
    A, h = targets(two.a.slug), as_user(client, two.a.email)
    before = _b_state(owner_db, two)
    nobody = str(uuid7())
    for method, suffix, ok in (("GET", "", 200), ("POST", "/archive", 200)):
        r = client.request(method, f"{A}/{two.a.target}{suffix}", headers=h)
        assert r.status_code == ok, (method, suffix, r.text)  # own object first
        _same_404(
            client.request(method, f"{A}/{two.b.target}{suffix}", headers=h),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h),
        )
    assert _b_state(owner_db, two) == before


def test_a_lists_only_its_own_rows(client: TestClient, two: Two) -> None:
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{targets(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [t["id"] for t in listed.json()["items"]]
        assert ids == [me.target] and other.target not in ids


# ------------------------------------------------------------------ executors
@dataclass(frozen=True)
class ExecSide:
    slug: str
    email: str
    executor: str
    credential: str
    token: str


@dataclass(frozen=True)
class TwoExecutors:
    a: ExecSide
    b: ExecSide


@pytest.fixture
def two_executors(client: TestClient, world: World, people: People) -> TwoExecutors:
    sides = []
    for c, email in ((world.b, people.owner_b), (world.a, people.owner_a)):
        h = as_user(client, email)
        made = create_executor(client, h, c.slug)
        out = issue(client, h, c.slug, made["id"])
        sides.append(ExecSide(c.slug, email, made["id"], out["credential"]["id"], out["secret"]))
    client.cookies.clear()
    return TwoExecutors(sides[1], sides[0])


def _b_executor_state(owner_db: Database, two: TwoExecutors) -> tuple[Any, ...]:
    with owner_db.session() as o:
        executor = o.execute(
            text(
                "SELECT name, status, last_seen_at, version, updated_at "
                "FROM shifde.delivery_executors WHERE id = :i"
            ),
            {"i": two.b.executor},
        ).one()
        creds = o.execute(
            text(
                "SELECT id, prefix, secret_hash, expires_at, revoked_at, updated_at "
                "FROM shifde.delivery_executor_credentials WHERE executor_id = :i ORDER BY id"
            ),
            {"i": two.b.executor},
        ).all()
    return tuple(executor), tuple(tuple(c) for c in creds)


def test_bs_executors_are_unknown_to_a(
    client: TestClient, owner_db: Database, two_executors: TwoExecutors
) -> None:
    two = two_executors
    h = as_user(client, two.a.email)
    url = f"{executors(two.a.slug)}/{two.a.executor}"
    assert client.patch(url, json={"status": "disabled"}, headers=h).status_code == 200
    own = client.post(executors(two.a.slug), json={"name": "own"}, headers=h)
    assert own.status_code == 201, own.text  # own organization first
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    ex, cred = two.b.executor, two.b.credential
    before = _b_executor_state(owner_db, two)
    for method, path, payload in (
        ("GET", "/delivery/executors", None),
        ("POST", "/delivery/executors", {"name": "intruder"}),
        ("GET", f"/delivery/executors/{ex}", None),
        ("PATCH", f"/delivery/executors/{ex}", {"status": "disabled"}),
        ("GET", f"/delivery/executors/{ex}/credentials", None),
        ("POST", f"/delivery/executors/{ex}/credentials", None),
        ("POST", f"/delivery/executors/{ex}/credentials/{cred}/rotate", None),
        ("POST", f"/delivery/executors/{ex}/credentials/{cred}/revoke", None),
    ):
        missing = client.request(method, f"{unknown}{path}", headers=h, json=payload)
        _same_404(
            client.request(method, f"{org(two.b.slug)}{path}", headers=h, json=payload), missing
        )
    assert _b_executor_state(owner_db, two) == before


def test_a_cannot_reach_bs_executor(
    client: TestClient, owner_db: Database, two_executors: TwoExecutors
) -> None:
    two = two_executors
    A, h = executors(two.a.slug), as_user(client, two.a.email)
    before = _b_executor_state(owner_db, two)
    nobody = str(uuid7())

    def check(method: str, suffix: str, ok: int, payload: Any = None) -> Any:
        """Own executor first, then B's executor and an unknown id under A's organization."""
        r = client.request(method, f"{A}/{two.a.executor}{suffix}", headers=h, json=payload)
        assert r.status_code == ok, (method, suffix, r.text)
        _same_404(
            client.request(method, f"{A}/{two.b.executor}{suffix}", headers=h, json=payload),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h, json=payload),
        )
        return r.json()

    check("GET", "", 200)
    check("PATCH", "", 200, {"status": "active"})
    check("GET", "/credentials", 200)
    rotated = check("POST", f"/credentials/{two.a.credential}/rotate", 201)
    check("POST", f"/credentials/{two.a.credential}/revoke", 200)
    check("POST", "/credentials", 201)
    # B's credential under A's own executor: the answer an unknown credential id gets
    base = f"{A}/{two.a.executor}/credentials"
    for action in ("rotate", "revoke"):
        _same_404(
            client.post(f"{base}/{two.b.credential}/{action}", headers=h),
            client.post(f"{base}/{nobody}/{action}", headers=h),
        )
    listed = {c["id"] for c in client.get(base, headers=h).json()}
    assert two.b.credential not in listed and rotated["credential"]["id"] in listed
    assert _b_executor_state(owner_db, two) == before


def test_a_lists_only_its_own_executors(client: TestClient, two_executors: TwoExecutors) -> None:
    two = two_executors
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{executors(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [e["id"] for e in listed.json()["items"]]
        assert ids == [me.executor] and other.executor not in ids
        creds = client.get(f"{executors(me.slug)}/{me.executor}/credentials", headers=h).json()
        assert [c["id"] for c in creds] == [me.credential]


def test_bs_executor_token_sees_only_bs_executor(
    client: TestClient, owner_db: Database, two_executors: TwoExecutors
) -> None:
    two = two_executors
    r = client.get(WHOAMI, headers={**bearer(two.b.token), "X-Executor-Version": "b-1"})
    assert r.status_code == 200, r.text
    assert r.json()["executor_id"] == two.b.executor and r.json()["version"] == "b-1"
    with owner_db.session() as o:
        seen = dict(
            o.execute(
                text(
                    "SELECT id::text, last_seen_at FROM shifde.delivery_executors "
                    "WHERE id IN (:a, :b)"
                ),
                {"a": two.a.executor, "b": two.b.executor},
            ).all()
        )
    assert seen[two.a.executor] is None and seen[two.b.executor] is not None


def test_a_shared_prefix_resolves_per_organization(
    client: TestClient,
    world: World,
    owner_db: Database,
    settings: Settings,
    two_executors: TwoExecutors,
) -> None:
    two = two_executors
    prefix = "Shared01"
    tokens = {}
    with owner_db.session() as o:
        for side, tenant in ((two.a, world.a.tenant), (two.b, world.b.tenant)):
            token = f"shx_{prefix}_{uuid7().hex}{'s' * 11}"
            tokens[side.executor] = token
            o.execute(
                text(
                    "INSERT INTO shifde.delivery_executor_credentials (id, tenant_id, executor_id, "
                    "prefix, secret_hash, created_by, expires_at) VALUES (:i, :t, :e, :p, :h, :c, "
                    "now() + interval '1 day')"
                ),
                {
                    "i": uuid7(),
                    "t": tenant,
                    "e": side.executor,
                    "p": prefix,
                    "h": hash_key(settings.pepper, token),
                    "c": uuid7(),
                },
            )
        o.commit()
    for executor, token in tokens.items():
        r = client.get(WHOAMI, headers=bearer(token))
        assert r.status_code == 200 and r.json()["executor_id"] == executor, r.text


# ------------------------------------------------------------------ plans
@dataclass(frozen=True)
class PlanSide:
    slug: str
    email: str
    tenant: UUID
    target: str
    plan: str
    release: str  # a sealed, passed release in the side's target's scope


@dataclass(frozen=True)
class TwoPlans:
    a: PlanSide
    b: PlanSide


@pytest.fixture
def two_plans(client: TestClient, world: World, two: Two, owner_db: Database) -> TwoPlans:
    sides = []
    for side, c in ((two.b, world.b), (two.a, world.a)):
        rel = str(release_for_target(owner_db, side.target).id)
        made = request_plan(client, as_user(client, side.email), side.slug, side.target, rel)
        sides.append(PlanSide(side.slug, side.email, c.tenant, side.target, made["id"], rel))
    client.cookies.clear()
    return TwoPlans(sides[1], sides[0])


def _plan_body(target: str, release: str) -> dict[str, str]:
    return {
        "target_id": target,
        "release_id": release,
        "application_key": "intruder",
        "display_name": "Intruder",
    }


def _b_plan_state(owner_db: Database, two: TwoPlans) -> tuple[Any, ...]:
    with owner_db.session() as o:
        plan = o.execute(
            text(
                "SELECT status, status_reason, digest, sealed_at, updated_at "
                "FROM shifde.delivery_plans WHERE id = :i"
            ),
            {"i": two.b.plan},
        ).one()
        counts = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_approvals WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_plans WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t), "
                "(SELECT count(*) FROM shifde.delivery_artifacts WHERE tenant_id = :t)"
            ),
            {"t": two.b.tenant},
        ).one()
    return tuple(plan), tuple(counts)


def test_a_cannot_reach_bs_plan(
    client: TestClient, owner_db: Database, two_plans: TwoPlans
) -> None:
    two = two_plans
    A, h = plans(two.a.slug), as_user(client, two.a.email)
    before = _b_plan_state(owner_db, two)
    nobody = str(uuid7())
    digest = {"plan_digest": "a" * 64}
    # own plan first: read, and the requester's own decision refused (403, not 404)
    for method, suffix, ok, payload in (
        ("GET", "", 200, None),
        ("POST", "/approve", 403, digest),
        ("POST", "/reject", 403, {}),
    ):
        r = client.request(method, f"{A}/{two.a.plan}{suffix}", headers=h, json=payload)
        assert r.status_code == ok, (method, suffix, r.text)
        _same_404(
            client.request(method, f"{A}/{two.b.plan}{suffix}", headers=h, json=payload),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h, json=payload),
        )
    # B's slug in the path: the unknown-organization 404
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    for method, path, payload in (
        ("GET", "/delivery/plans", None),
        ("POST", "/delivery/plans", _plan_body(two.b.target, two.b.release)),
        ("GET", f"/delivery/plans/{two.b.plan}", None),
        ("POST", f"/delivery/plans/{two.b.plan}/approve", digest),
        ("POST", f"/delivery/plans/{two.b.plan}/reject", {}),
    ):
        _same_404(
            client.request(method, f"{org(two.b.slug)}{path}", headers=h, json=payload),
            client.request(method, f"{unknown}{path}", headers=h, json=payload),
        )
    assert _b_plan_state(owner_db, two) == before


def test_a_cannot_reach_bs_sealed_plan(
    client: TestClient, world: World, people: People, owner_db: Database, two_plans: TwoPlans
) -> None:
    """B's sealed plan: A's read, approval (quoting B's real digest) and rejection each get the
    unknown-id 404, and B's plan is unchanged."""
    two = two_plans
    b = make_executor(client, world.b, people.owner_b)
    leased = lease_plan(client, b.token)
    assert leased["job"]["plan_id"] == two.b.plan
    assert plan_result(client, b.token, leased).status_code == 200
    with owner_db.session() as o:
        status, sealed_digest = o.execute(
            text("SELECT status, digest FROM shifde.delivery_plans WHERE id = :i"),
            {"i": two.b.plan},
        ).one()
    assert status == "sealed" and sealed_digest
    before = _b_plan_state(owner_db, two)
    A, h = plans(two.a.slug), as_user(client, two.a.email)
    nobody = str(uuid7())
    for method, suffix, payload in (
        ("GET", "", None),
        ("POST", "/approve", {"plan_digest": sealed_digest}),
        ("POST", "/reject", {}),
    ):
        _same_404(
            client.request(method, f"{A}/{two.b.plan}{suffix}", headers=h, json=payload),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h, json=payload),
        )
    assert _b_plan_state(owner_db, two) == before


def test_a_cannot_plan_on_bs_target(
    client: TestClient, owner_db: Database, two_plans: TwoPlans
) -> None:
    two = two_plans
    h = as_user(client, two.a.email)
    request_plan(client, h, two.a.slug, two.a.target, two.a.release)  # own target first
    before = _b_plan_state(owner_db, two)
    _same_404(
        client.post(plans(two.a.slug), json=_plan_body(two.b.target, two.a.release), headers=h),
        client.post(plans(two.a.slug), json=_plan_body(str(uuid7()), two.a.release), headers=h),
    )
    assert _b_plan_state(owner_db, two) == before
    with owner_db.session() as o:
        n = o.execute(
            text("SELECT count(*) FROM shifde.delivery_plans WHERE target_id = :t"),
            {"t": two.b.target},
        ).scalar_one()
    assert n == 1  # B's own plan only


def _a_writes(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:
    with owner_db.session() as o:
        return tuple(
            o.execute(
                text(
                    "SELECT (SELECT count(*) FROM shifde.delivery_plans WHERE tenant_id = :t), "
                    "(SELECT count(*) FROM shifde.delivery_artifacts WHERE tenant_id = :t), "
                    "(SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t)"
                ),
                {"t": tenant},
            ).one()
        )


def _b_release_state(owner_db: Database, release: str) -> tuple[Any, ...]:
    with owner_db.session() as o:
        rel = o.execute(text("SELECT * FROM shifde.releases WHERE id = :r"), {"r": release}).one()
        runs = o.execute(
            text("SELECT * FROM shifde.evaluation_runs WHERE release_id = :r ORDER BY id"),
            {"r": release},
        ).all()
    return tuple(rel), tuple(tuple(r) for r in runs)


def test_a_cannot_name_bs_release(
    client: TestClient, owner_db: Database, two_plans: TwoPlans
) -> None:
    """A's plan on A's own target naming B's sealed, passed release: byte for byte the 404 an
    unknown release id gets; nothing is written for A and B's release and runs are unchanged."""
    two = two_plans
    h = as_user(client, two.a.email)
    request_plan(client, h, two.a.slug, two.a.target, two.a.release)  # own release first
    a_before, b_before = _a_writes(owner_db, two.a.tenant), _b_plan_state(owner_db, two)
    release_before = _b_release_state(owner_db, two.b.release)
    named = client.post(plans(two.a.slug), json=_plan_body(two.a.target, two.b.release), headers=h)
    unknown = client.post(plans(two.a.slug), json=_plan_body(two.a.target, str(uuid7())), headers=h)
    _same_404(named, unknown)
    assert named.json()["detail"] == "release not found"
    assert named.content == unknown.content
    assert _a_writes(owner_db, two.a.tenant) == a_before
    assert _b_plan_state(owner_db, two) == b_before
    assert _b_release_state(owner_db, two.b.release) == release_before


def test_a_lists_only_its_own_plans(client: TestClient, two_plans: TwoPlans) -> None:
    two = two_plans
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{plans(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [p["id"] for p in listed.json()["items"]]
        assert ids == [me.plan] and other.plan not in ids
        by_target = client.get(plans(me.slug), params={"target": other.target}, headers=h)
        assert by_target.status_code == 200 and by_target.json()["items"] == []


def test_bs_job_cannot_name_as_plan(
    owner_db: Database, app_db: Database, two_plans: TwoPlans
) -> None:
    """B's job naming A's plan: the service refuses it as an unknown plan (B-2732), and the
    composite key `fk_delivery_jobs__plan` refuses a direct insert (B-2673)."""
    two = two_plans

    def a_plan() -> tuple[Any, ...]:
        with owner_db.session() as o:
            row = o.execute(
                text("SELECT * FROM shifde.delivery_plans WHERE id = :i"), {"i": two.a.plan}
            ).one()
        return tuple(row)

    def b_jobs() -> int:
        with owner_db.session() as o:
            n: int = o.execute(
                text("SELECT count(*) FROM shifde.delivery_jobs WHERE tenant_id = :t"),
                {"t": two.b.tenant},
            ).scalar_one()
        return n

    before, b_before = a_plan(), b_jobs()
    plan_id, target_id = UUID(two.a.plan), UUID(two.b.target)
    with app_session(app_db, two.b.tenant) as s:
        with pytest.raises(NotFound):
            gateway.enqueue_job(
                s, tenant_id=two.b.tenant, kind="plan", plan_id=plan_id, target_id=target_id
            )
        s.commit()
    assert b_jobs() == b_before
    with app_session(app_db, two.b.tenant) as s, pytest.raises(IntegrityError) as exc:
        repository.insert_job(
            s,
            {
                "tenant_id": two.b.tenant,
                "id": uuid7(),
                "kind": "plan",
                "plan_id": plan_id,
                "deployment_id": None,
                "target_id": target_id,
                "run_after": None,
                "deadline": None,
                "max_attempts": service.READ_ONLY_MAX_ATTEMPTS,
            },
        )
    assert "fk_delivery_jobs__plan" in str(exc.value)
    assert a_plan() == before
    assert b_jobs() == b_before
    with owner_db.session() as o:
        jobs = o.execute(
            text("SELECT tenant_id FROM shifde.delivery_jobs WHERE plan_id = :i"),
            {"i": two.a.plan},
        ).all()
    assert [j.tenant_id for j in jobs] == [two.a.tenant]  # only A's own plan job


def test_bs_executor_cannot_fetch_as_plan_artifact(
    client: TestClient, world: World, people: People, two_plans: TwoPlans
) -> None:
    two = two_plans
    a = make_executor(client, world.a, people.owner_a)
    b = make_executor(client, world.b, people.owner_b)
    leased = lease_plan(client, a.token)
    assert leased["job"]["plan_id"] == two.a.plan
    url = job_url(leased["job"]["id"], "artifact")
    assert client.get(url, params=proof(leased), headers=bearer(a.token)).status_code == 200
    _same_404(
        client.get(url, params=proof(leased), headers=bearer(b.token)),
        client.get(job_url(uuid7(), "artifact"), params=proof(leased), headers=bearer(b.token)),
    )
    # B's own lease returns B's plan job, never A's
    assert lease_plan(client, b.token)["job"]["plan_id"] == two.b.plan


# ------------------------------------------------------------------ deployments
@dataclass(frozen=True)
class DeploymentSide:
    slug: str
    email: str
    tenant: UUID
    deployment: UUID
    apply_job: UUID


@dataclass(frozen=True)
class TwoDeployments:
    a: DeploymentSide
    b: DeploymentSide


@pytest.fixture
def two_deployments(
    world: World, people: People, owner_db: Database, app_db: Database
) -> TwoDeployments:
    """A queued deployment with a queued apply job in each organization."""
    sides = []
    for c, email in ((world.b, people.owner_b), (world.a, people.owner_a)):
        deployment = make_deployment(owner_db, c.tenant)
        job = enqueue(app_db, c.tenant, "apply", deployment_id=deployment)
        sides.append(DeploymentSide(c.slug, email, c.tenant, deployment, job))
    return TwoDeployments(sides[1], sides[0])


def _b_deployment_state(owner_db: Database, two: TwoDeployments) -> tuple[Any, ...]:
    with owner_db.session() as o:
        deployment = o.execute(
            text("SELECT * FROM shifde.delivery_deployments WHERE id = :i"),
            {"i": two.b.deployment},
        ).one()
        jobs = o.execute(
            text("SELECT * FROM shifde.delivery_jobs WHERE tenant_id = :t ORDER BY id"),
            {"t": two.b.tenant},
        ).all()
        journal = o.execute(
            text("SELECT * FROM shifde.delivery_journal WHERE tenant_id = :t"),
            {"t": two.b.tenant},
        ).all()
    return tuple(deployment), tuple(tuple(j) for j in jobs), tuple(tuple(j) for j in journal)


def _a_deployment_state(owner_db: Database, two: TwoDeployments) -> tuple[Any, ...]:
    flipped = TwoDeployments(two.b, two.a)
    return _b_deployment_state(owner_db, flipped)


def test_a_cannot_reach_bs_deployment(
    client: TestClient, owner_db: Database, two_deployments: TwoDeployments
) -> None:
    two = two_deployments
    A, h = deployments(two.a.slug), as_user(client, two.a.email)
    before = _b_deployment_state(owner_db, two)
    nobody = str(uuid7())
    note = {"note": "intruder"}
    # own deployment first: read it, the state refusals (409), then cancel it (200)
    for method, suffix, ok, payload in (
        ("GET", "", 200, None),
        ("POST", "/reconcile", 409, None),
        ("POST", "/mark-failed", 409, note),
        ("POST", "/cancel", 200, None),
    ):
        r = client.request(method, f"{A}/{two.a.deployment}{suffix}", headers=h, json=payload)
        assert r.status_code == ok, (method, suffix, r.text)
        _same_404(
            client.request(method, f"{A}/{two.b.deployment}{suffix}", headers=h, json=payload),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h, json=payload),
        )
    # B's slug in the path: the unknown-organization 404
    unknown = org(f"no-such-{uuid7().hex[-12:]}")
    for method, path, payload in (
        ("GET", "/delivery/deployments", None),
        ("GET", f"/delivery/deployments/{two.b.deployment}", None),
        ("POST", f"/delivery/deployments/{two.b.deployment}/cancel", None),
        ("POST", f"/delivery/deployments/{two.b.deployment}/reconcile", None),
        ("POST", f"/delivery/deployments/{two.b.deployment}/mark-failed", note),
    ):
        _same_404(
            client.request(method, f"{org(two.b.slug)}{path}", headers=h, json=payload),
            client.request(method, f"{unknown}{path}", headers=h, json=payload),
        )
    assert _b_deployment_state(owner_db, two) == before


def test_a_lists_only_its_own_deployments(
    client: TestClient, two_deployments: TwoDeployments
) -> None:
    two = two_deployments
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{deployments(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [d["id"] for d in listed.json()["items"]]
        assert ids == [str(me.deployment)] and str(other.deployment) not in ids


def test_bs_job_and_journal_cannot_name_as_deployment(
    owner_db: Database, app_db: Database, two_deployments: TwoDeployments
) -> None:
    """B-2673: `fk_delivery_jobs__deployment` and `fk_delivery_journal__deployment` refuse B's
    job and B's journal step naming A's deployment; A's rows stay as they were."""
    two = two_deployments
    before = _a_deployment_state(owner_db, two)
    with app_session(app_db, two.b.tenant) as s, pytest.raises(IntegrityError) as exc:
        gateway.enqueue_job(
            s, tenant_id=two.b.tenant, kind="observe", deployment_id=two.a.deployment
        )
    assert "fk_delivery_jobs__deployment" in str(exc.value)
    with app_session(app_db, two.b.tenant) as s, pytest.raises(IntegrityError) as exc:
        repository.insert_intent(
            s,
            {
                "tenant_id": two.b.tenant,
                "deployment_id": two.a.deployment,
                "step_key": "deploy_app",
                "input_digest": "a" * 64,
                "job_id": two.b.apply_job,
                "fence": 1,
            },
        )
    assert "fk_delivery_journal__deployment" in str(exc.value)
    assert _a_deployment_state(owner_db, two) == before
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT (SELECT array_agg(DISTINCT tenant_id) FROM shifde.delivery_jobs "
                "WHERE deployment_id = :d), (SELECT count(*) FROM shifde.delivery_journal "
                "WHERE deployment_id = :d)"
            ),
            {"d": two.a.deployment},
        ).one()
    assert rows[0] == [two.a.tenant] and rows[1] == 0


def test_bs_native_resource_cannot_name_as_deployment_or_target(
    owner_db: Database, app_db: Database, two_deployments: TwoDeployments
) -> None:
    """`fk_delivery_native_resources__deployment` and `fk_delivery_native_resources__target`
    refuse B's native resource row naming A's deployment or A's target (each insert wrong in one
    column); B's own row is accepted and A's rows stay as they were."""
    two = two_deployments
    a_target = deployment_row(owner_db, two.a.deployment)["target_id"]
    b_target = deployment_row(owner_db, two.b.deployment)["target_id"]
    before = _a_deployment_state(owner_db, two)

    def row(target_id: UUID, deployment_id: UUID) -> dict[str, Any]:
        return {
            "tenant_id": two.b.tenant,
            "target_id": target_id,
            "application_key": "helper-app",
            "resource_type": "databricks_app",
            "native_id": f"app-{uuid7().hex[-12:]}",
            "deployment_id": deployment_id,
        }

    for values, key in (
        (row(b_target, two.a.deployment), "fk_delivery_native_resources__deployment"),
        (row(a_target, two.b.deployment), "fk_delivery_native_resources__target"),
    ):
        with app_session(app_db, two.b.tenant) as s, pytest.raises(IntegrityError) as exc:
            repository.insert_native_resource(s, values)
        assert key in str(exc.value)
    with app_session(app_db, two.b.tenant) as s:  # B's own target and deployment
        repository.insert_native_resource(s, row(b_target, two.b.deployment))
        s.commit()
    with owner_db.session() as o:
        rows = o.execute(
            text(
                "SELECT tenant_id, target_id, deployment_id FROM shifde.delivery_native_resources "
                "WHERE tenant_id = :t OR target_id = :g OR deployment_id = :d"
            ),
            {"t": two.b.tenant, "g": a_target, "d": two.a.deployment},
        ).all()
    assert [tuple(r) for r in rows] == [(two.b.tenant, b_target, two.b.deployment)]
    assert _a_deployment_state(owner_db, two) == before


def test_bs_executor_leases_none_of_as_apply_jobs(
    client: TestClient,
    world: World,
    people: People,
    owner_db: Database,
    two_deployments: TwoDeployments,
) -> None:
    two = two_deployments
    a = make_executor(client, world.a, people.owner_a)
    b = make_executor(client, world.b, people.owner_b)
    before = _a_deployment_state(owner_db, two)
    r = lease(client, b.token, kinds=["apply"])
    assert r.status_code == 200 and r.json()["job"]["id"] == str(two.b.apply_job), r.text
    assert lease(client, b.token, kinds=["apply"]).status_code == 204
    assert _a_deployment_state(owner_db, two) == before
    r = lease(client, a.token, kinds=["apply"])
    assert r.status_code == 200 and r.json()["job"]["id"] == str(two.a.apply_job), r.text


# ------------------------------------------------------------------ settle scope
_ROWS = (
    "SELECT * FROM shifde.delivery_plans WHERE tenant_id = :t ORDER BY id",
    "SELECT * FROM shifde.delivery_deployments WHERE tenant_id = :t ORDER BY id",
    "SELECT * FROM shifde.delivery_jobs WHERE tenant_id = :t ORDER BY id",
    "SELECT * FROM shifde.delivery_journal WHERE tenant_id = :t ORDER BY deployment_id, step_key",
)


def _rows(owner_db: Database, tenant: UUID) -> tuple[Any, ...]:
    """Every plan, deployment, job and journal row of `tenant`, whole, read as the owner."""
    with owner_db.session() as o:
        return tuple(
            tuple(tuple(r) for r in o.execute(text(sql), {"t": tenant}).all()) for sql in _ROWS
        )


def test_as_settle_step_leaves_bs_rows_unchanged(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """The settle step runs for the caller's organization only: B holds a lost apply after an
    intent (not yet swept), a sealed plan past `expires_at` and an `unknown_outcome` deployment
    with a queued apply on its target; A's lease and A's plan and deployment list and get leave
    every B row byte for byte as it was, and B's own next call settles them."""
    flows = {}
    for c, email in ((world.a, people.owner_a), (world.b, people.owner_b)):
        token = make_executor(client, c, email).token
        approver = system_role_user(owner_db, c.tenant, "tenant_administrator")
        flows[c.slug] = Flow(client, c.slug, ws_slug(c), c.tenant, email, approver, token, owner_db)
    a, b = flows[world.a.slug], flows[world.b.slug]
    # B: every plan and deployment first (a target with an unknown outcome takes no new plan)
    held, other = b.target()["id"], b.target()["id"]
    _, unknown = b.deployment(held)
    _, queued = b.deployment(held)
    plan, lost = b.deployment(other)
    leased = lease_kind(client, b.token, "apply")
    assert leased["job"]["deployment_id"] == unknown
    assert checkpoint(client, b.token, leased, 1, "intent").status_code == 200
    expire(owner_db, leased["job"]["id"])
    assert b.get(unknown)["status"] == "unknown_outcome"  # B's own settle step holds `queued`
    leased = lease_kind(client, b.token, "apply")
    assert leased["job"]["deployment_id"] == lost
    assert checkpoint(client, b.token, leased, 1, "intent").status_code == 200
    expire(owner_db, leased["job"]["id"])  # lost, not yet swept in B's organization
    lost_job = leased["job"]["id"]
    expired_plan = uuid7()  # sealed 20 minutes ago (the seal guard keeps a plan from moving back)
    owner_sql(
        owner_db,
        "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, target_revision, "
        "artifact_id, artifact_digest, application_key, display_name, status, requested_by, "
        "steps, snapshot, adapter_version, toolchain_digest, digest, sealed_at, expires_at) "
        "SELECT tenant_id, :id, target_id, target_revision, artifact_id, artifact_digest, "
        "application_key, display_name, 'sealed', requested_by, steps, snapshot, "
        "adapter_version, toolchain_digest, digest, now() - interval '20 minutes', "
        "now() - interval '5 minutes' FROM shifde.delivery_plans WHERE id = :p",
        id=expired_plan,
        p=plan["id"],
    )
    (queued_job,) = deployment_jobs(owner_db, queued, "apply")
    assert queued_job["status"] == "queued" and queued_job["run_after"] > queued_job["db_now"]
    assert deployment_row(owner_db, lost)["status"] == "applying"
    assert plan_row(owner_db, expired_plan)["status"] == "sealed"
    # A: its own plan and deployment, then the calls that run the settle step
    a_plan, a_deployment = a.deployment()
    before = _rows(owner_db, world.b.tenant)
    r = lease(client, a.token)
    assert r.status_code == 200 and r.json()["job"]["deployment_id"] == a_deployment, r.text
    h = as_user(client, a.requester)
    for url in (
        plans(a.slug),
        f"{plans(a.slug)}/{a_plan['id']}",
        deployments(a.slug),
        f"{deployments(a.slug)}/{a_deployment}",
    ):
        assert client.get(url, headers=h).status_code == 200, url
    client.cookies.clear()
    assert _rows(owner_db, world.b.tenant) == before
    # B's own call settles the pending work: the setup really was pending
    h = as_user(client, b.requester)
    assert client.get(deployments(b.slug), headers=h).status_code == 200
    client.cookies.clear()
    assert _rows(owner_db, world.b.tenant) != before
    with owner_db.session() as o:
        job_status = o.execute(
            text("SELECT status FROM shifde.delivery_jobs WHERE id = :j"), {"j": lost_job}
        ).scalar_one()
    assert job_status == "lost"
    assert plan_row(owner_db, expired_plan)["status"] == "expired"
    assert deployment_row(owner_db, lost)["status"] == "unknown_outcome"


# ------------------------------------------------------------------ Snowflake targets (SP6-3)
@pytest.fixture
def two_snowflake(client: TestClient, world: World, people: People) -> Two:
    """A Snowflake target of each organization."""
    sides = []
    for c, email in ((world.b, people.owner_b), (world.a, people.owner_a)):
        body = snowflake_body(ws_slug(c), snowflake_address())
        made = register(client, as_user(client, email), c.slug, body)
        sides.append(Side(c.slug, email, made["id"]))
    return Two(sides[1], sides[0])


def test_a_cannot_reach_bs_snowflake_target(
    client: TestClient, owner_db: Database, two_snowflake: Two
) -> None:
    two = two_snowflake
    A, h = targets(two.a.slug), as_user(client, two.a.email)
    before = _b_state(owner_db, two)
    nobody = str(uuid7())
    for method, suffix, ok in (("GET", "", 200), ("POST", "/archive", 200)):
        r = client.request(method, f"{A}/{two.a.target}{suffix}", headers=h)
        assert r.status_code == ok, (method, suffix, r.text)  # own object first
        _same_404(
            client.request(method, f"{A}/{two.b.target}{suffix}", headers=h),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h),
        )
    assert _b_state(owner_db, two) == before


def test_a_lists_none_of_bs_snowflake_targets(client: TestClient, two_snowflake: Two) -> None:
    two = two_snowflake
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{targets(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [t["id"] for t in listed.json()["items"]]
        assert ids == [me.target] and other.target not in ids


def test_bs_executor_leases_none_of_as_snowflake_plan_jobs(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    two_snowflake: Two,
) -> None:
    a = make_executor(client, world.a, people.owner_a)
    b = make_executor(client, world.b, people.owner_b)
    plan = snowflake_plan(app_db, world.a.tenant, two_snowflake.a.target)
    before = job_row(owner_db, plan.job_id)
    r = lease(client, b.token, kinds=["plan"])
    assert r.status_code == 204, r.text
    assert job_row(owner_db, plan.job_id) == before
    r = lease(client, a.token, kinds=["plan"])
    assert r.status_code == 200 and r.json()["job"]["id"] == str(plan.job_id), r.text


# ------------------------------------------------------------------ Fabric, Palantir, Docker (SP8-1)
def _two_sp8(client: TestClient, world: World, people: People, kind: str) -> Two:
    """A target of `kind` in each organization."""
    sides = []
    for c, email in ((world.b, people.owner_b), (world.a, people.owner_a)):
        body = sp8_body(kind, ws_slug(c), sp8_address(kind))
        made = register(client, as_user(client, email), c.slug, body)
        assert made["platform_kind"] == kind
        sides.append(Side(c.slug, email, made["id"]))
    return Two(sides[1], sides[0])


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_a_cannot_reach_bs_sp8_target(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    two = _two_sp8(client, world, people, kind)
    A, h = targets(two.a.slug), as_user(client, two.a.email)
    before = _b_state(owner_db, two)
    nobody = str(uuid7())
    for method, suffix, ok in (("GET", "", 200), ("POST", "/archive", 200)):
        r = client.request(method, f"{A}/{two.a.target}{suffix}", headers=h)
        assert r.status_code == ok, (method, suffix, r.text)  # own object first
        _same_404(
            client.request(method, f"{A}/{two.b.target}{suffix}", headers=h),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h),
        )
    assert _b_state(owner_db, two) == before


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_each_side_lists_only_its_own_sp8_targets(
    client: TestClient, world: World, people: People, kind: str
) -> None:
    two = _two_sp8(client, world, people, kind)
    for me, other in ((two.a, two.b), (two.b, two.a)):
        h = as_user(client, me.email)
        listed = client.get(f"{targets(me.slug)}?limit=200", headers=h)
        assert listed.status_code == 200, listed.text
        ids = [t["id"] for t in listed.json()["items"]]
        assert ids == [me.target] and other.target not in ids


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_bs_executor_leases_none_of_as_sp8_plan_jobs(
    client: TestClient,
    world: World,
    people: People,
    app_db: Database,
    owner_db: Database,
    kind: str,
) -> None:
    two = _two_sp8(client, world, people, kind)
    a = make_executor(client, world.a, people.owner_a)
    b = make_executor(client, world.b, people.owner_b)
    plan = sp8_plan(app_db, world.a.tenant, two.a.target)
    before = job_row(owner_db, plan.job_id)
    r = lease(client, b.token, kinds=["plan"])
    assert r.status_code == 204, r.text
    assert job_row(owner_db, plan.job_id) == before
    r = lease(client, a.token, kinds=["plan"])
    assert r.status_code == 200 and r.json()["job"]["id"] == str(plan.job_id), r.text


@pytest.mark.parametrize("kind", SP8_KINDS)
def test_a_p1_only_member_cannot_reach_p2s_sp8_target(
    client: TestClient, world: World, people: People, owner_db: Database, kind: str
) -> None:
    """Within one organization: a member bound only to project P1 gets on P2's target the 404 an
    unknown id gets (GET and archive), and P2's target is absent from the member's list."""
    a = world.a
    p2 = add_project(owner_db, a.tenant, a.ws)
    ha = as_user(client, people.owner_a)
    t1 = register(
        client,
        ha,
        a.slug,
        sp8_body(kind, ws_slug(a), sp8_address(kind), project=project_slug(a)),
    )
    t2 = register(
        client, ha, a.slug, sp8_body(kind, ws_slug(a), sp8_address(kind), project=f"p-{p2.hex}")
    )
    assert t1["project"] == project_slug(a) and t2["project"] == f"p-{p2.hex}"
    p2_state = _b_state(owner_db, Two(Side(a.slug, "", t1["id"]), Side(a.slug, "", t2["id"])))
    email = member(
        owner_db,
        a.tenant,
        ["delivery:target:read", "delivery:target:write"],
        scope_type="project",
        scope_id=a.project,
    )
    h = as_user(client, email)
    A, nobody = targets(a.slug), str(uuid7())
    listed = client.get(f"{A}?limit=200", headers=h)
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()["items"]] == [t1["id"]]
    for method, suffix in (("GET", ""), ("POST", "/archive")):
        _same_404(
            client.request(method, f"{A}/{t2['id']}{suffix}", headers=h),
            client.request(method, f"{A}/{nobody}{suffix}", headers=h),
        )
    assert (
        _b_state(owner_db, Two(Side(a.slug, "", t1["id"]), Side(a.slug, "", t2["id"]))) == p2_state
    )
    # the member's own project's target answers (and archives) normally
    assert client.get(f"{A}/{t1['id']}", headers=h).status_code == 200
    assert client.post(f"{A}/{t1['id']}/archive", headers=h).status_code == 200


# ------------------------------------------------------------------ generated Snowflake apps (SP6-6)
@dataclass(frozen=True)
class SnowflakeSide:
    flow: Flow
    target: str
    release: AppRelease
    sealed: dict[str, Any]  # a sealed plan
    approved: dict[str, Any]  # another plan, approved
    deployment: str  # its queued deployment


@pytest.fixture
def two_snowflake_apps(
    client: TestClient, world: World, people: People, owner_db: Database
) -> dict[str, SnowflakeSide]:
    """In each organization: a Snowflake target, a sealed, passed release of its own, a sealed plan
    and an approved one with its queued deployment, all through the routes."""
    sides = {}
    for name, c, email in (("b", world.b, people.owner_b), ("a", world.a, people.owner_a)):
        flow = Flow(
            client,
            c.slug,
            ws_slug(c),
            c.tenant,
            email,
            system_role_user(owner_db, c.tenant, "tenant_administrator"),
            make_executor(client, c, email).token,
            owner_db,
        )
        target = app_target(flow)
        release = app_release(flow, target)
        approved = flow.approve(flow.sealed(target["id"], "live-app", release_id=release.id))
        sealed = flow.sealed(target["id"], release_id=release.id)
        assert sealed["status"] == "sealed" and approved["deployment_id"]
        sides[name] = SnowflakeSide(
            flow, target["id"], release, sealed, approved, approved["deployment_id"]
        )
    return sides


_SNOWFLAKE_TABLES = (
    "delivery_targets",
    "delivery_artifacts",
    "delivery_plans",
    "delivery_deployments",
)


def _tenant_rows(owner_db: Database, side: SnowflakeSide) -> list[list[tuple[Any, ...]]]:
    """The organization's delivery rows and release, read as the schema owner."""
    tables = (*_SNOWFLAKE_TABLES, "delivery_jobs", "delivery_approvals", "releases")
    with owner_db.session() as o:
        return [
            [
                tuple(r)
                for r in o.execute(
                    # the table names are the literals above
                    text(f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY id"),  # nosec B608
                    {"t": side.flow.tenant},
                )
            ]
            for table in tables
        ]


@pytest.mark.parametrize(("me", "other"), [("a", "b"), ("b", "a")])
def test_a_cannot_plan_bs_snowflake_target_or_release(
    client: TestClient,
    owner_db: Database,
    two_snowflake_apps: dict[str, SnowflakeSide],
    me: str,
    other: str,
) -> None:
    mine, theirs = two_snowflake_apps[me], two_snowflake_apps[other]
    flow, release = mine.flow, str(mine.release.id)
    h = as_user(client, flow.requester)
    request_plan(client, h, flow.slug, mine.target, release, application_key="own")  # own first
    before, own = _tenant_rows(owner_db, theirs), _tenant_rows(owner_db, mine)
    nobody = str(uuid7())
    for named, unknown in (
        (_plan_body(theirs.target, release), _plan_body(nobody, release)),
        (_plan_body(theirs.target, str(theirs.release.id)), _plan_body(nobody, release)),
        (_plan_body(mine.target, str(theirs.release.id)), _plan_body(mine.target, nobody)),
    ):
        _same_404(
            client.post(plans(flow.slug), json=named, headers=h),
            client.post(plans(flow.slug), json=unknown, headers=h),
        )
    assert _tenant_rows(owner_db, theirs) == before
    assert _tenant_rows(owner_db, mine) == own


@pytest.mark.parametrize(("me", "other"), [("a", "b"), ("b", "a")])
def test_a_cannot_reach_bs_snowflake_plan_or_deployment(
    client: TestClient,
    owner_db: Database,
    two_snowflake_apps: dict[str, SnowflakeSide],
    me: str,
    other: str,
) -> None:
    mine, theirs = two_snowflake_apps[me], two_snowflake_apps[other]
    slug = mine.flow.slug
    h = as_user(client, mine.flow.requester)
    before = _tenant_rows(owner_db, theirs)
    nobody = str(uuid7())
    digest = {"plan_digest": theirs.sealed["digest"]}
    note = {"note": "intruder"}
    own_digest = {"plan_digest": mine.sealed["digest"]}
    for base, own_id, their_id, calls in (
        (
            plans(slug),
            mine.sealed["id"],
            theirs.sealed["id"],
            (("GET", "", None), ("POST", "/approve", digest), ("POST", "/reject", {})),
        ),
        (
            deployments(slug),
            mine.deployment,
            theirs.deployment,
            (
                ("GET", "", None),
                ("POST", "/reconcile", None),
                ("POST", "/mark-failed", note),
                ("POST", "/cancel", None),
            ),
        ),
    ):
        for method, suffix, payload in calls:
            sent = own_digest if suffix == "/approve" else payload
            r = client.request(method, f"{base}/{own_id}{suffix}", headers=h, json=sent)
            assert r.status_code in (200, 403, 409), (suffix, r.text)  # own object first
            _same_404(
                client.request(method, f"{base}/{their_id}{suffix}", headers=h, json=payload),
                client.request(method, f"{base}/{nobody}{suffix}", headers=h, json=payload),
            )
    listed_plans = client.get(f"{plans(slug)}?limit=200", headers=h).json()["items"]
    assert {p["id"] for p in listed_plans} == {mine.sealed["id"], mine.approved["id"]}
    listed = client.get(f"{deployments(slug)}?limit=200", headers=h).json()["items"]
    assert [d["id"] for d in listed] == [mine.deployment]
    by_target = client.get(plans(slug), params={"target": theirs.target}, headers=h)
    assert by_target.json()["items"] == []
    assert _tenant_rows(owner_db, theirs) == before


@pytest.mark.parametrize(("me", "other"), [("a", "b"), ("b", "a")])
def test_as_snowflake_release_cannot_use_bs_provider(
    client: TestClient,
    owner_db: Database,
    two_snowflake_apps: dict[str, SnowflakeSide],
    me: str,
    other: str,
) -> None:
    """A provider of the other organization, its `base_url` on my own target's account: refused
    like an unknown id, naming nothing of theirs."""
    mine, theirs = two_snowflake_apps[me], two_snowflake_apps[other]
    flow = mine.flow
    h = as_user(client, flow.requester)
    target = client.get(f"{targets(flow.slug)}/{mine.target}", headers=h).json()
    client.cookies.clear()
    answers = []
    for provider in (
        add_provider(
            owner_db, theirs.flow.tenant, "openai_compatible", f"{target['address']}/api/v2"
        ),
        theirs.release.provider_id,
        uuid7(),
    ):
        release = app_release(flow, target, provider_id=provider)
        before, own = _tenant_rows(owner_db, theirs), _tenant_rows(owner_db, mine)
        h = as_user(client, flow.requester)
        body = request_plan(
            client, h, flow.slug, mine.target, release.id, 409, application_key="own"
        )
        client.cookies.clear()
        assert body["code"] == "model_not_in_target_account"
        assert str(provider) not in str(body) and target["address"] not in str(body)
        assert _tenant_rows(owner_db, theirs) == before
        assert _tenant_rows(owner_db, mine) == own
        answers.append({k: v for k, v in body.items() if k not in _VARYING})
    assert answers[0] == answers[1] == answers[2]


@pytest.mark.parametrize(("me", "other"), [("a", "b"), ("b", "a")])
def test_shifde_app_in_as_context_selects_none_of_bs_snowflake_rows(
    app_db: Database, two_snowflake_apps: dict[str, SnowflakeSide], me: str, other: str
) -> None:
    mine, theirs = two_snowflake_apps[me], two_snowflake_apps[other]
    with app_session(app_db, mine.flow.tenant) as s:
        for table in _SNOWFLAKE_TABLES:
            # the table names are the literals above
            found = s.execute(text(f"SELECT DISTINCT tenant_id FROM shifde.{table}"))  # nosec B608
            assert set(found.scalars()) == {mine.flow.tenant}, table
        for table, their_id in (
            ("delivery_targets", theirs.target),
            ("delivery_artifacts", theirs.sealed["artifact_id"]),
            ("delivery_plans", theirs.sealed["id"]),
            ("delivery_deployments", theirs.deployment),
        ):
            n = s.execute(
                text(f"SELECT count(*) FROM shifde.{table} WHERE id = :i"),  # nosec B608
                {"i": their_id},
            ).scalar_one()
            assert n == 0, table


# ------------------------------------------------------------------ generated Docker apps (SP8-9)
@dataclass(frozen=True)
class DockerSide:
    flow: Flow
    workspace: dict[str, Any]  # the Databricks target whose claim the Docker target needs
    target: str
    release: AppRelease
    sealed: dict[str, Any]  # a sealed plan
    approved: dict[str, Any]  # another plan, approved
    deployment: str  # its queued deployment


@pytest.fixture
def two_docker_apps(
    client: TestClient, world: World, people: People, owner_db: Database
) -> dict[str, DockerSide]:
    """In each organization: a claimed workspace, a Docker target of it (the same project and port
    on each one's own daemon), a release, a sealed plan and an approved one with its queued
    deployment, all through the routes."""
    sides = {}
    for name, c, email in (("b", world.b, people.owner_b), ("a", world.a, people.owner_a)):
        flow = Flow(
            client,
            c.slug,
            ws_slug(c),
            c.tenant,
            email,
            system_role_user(owner_db, c.tenant, "tenant_administrator"),
            make_executor(client, c, email).token,
            owner_db,
        )
        held = workspace_target(flow)
        target = docker_target(flow, workspace_host=held["address"])
        release = docker_release(flow, target)
        approved = flow.approve(flow.sealed(target["id"], "live-app", release_id=release.id))
        sealed = flow.sealed(target["id"], release_id=release.id)
        assert sealed["status"] == "sealed" and approved["deployment_id"]
        sides[name] = DockerSide(
            flow, held, target["id"], release, sealed, approved, approved["deployment_id"]
        )
    bound = [s.flow for s in sides.values()]
    assert bound[0].slug != bound[1].slug
    return sides


def _both(owner_db: Database, sides: dict[str, DockerSide]) -> list[Any]:
    return [rows(owner_db, side.flow.tenant) for side in sides.values()]


@pytest.mark.parametrize(("me", "other"), [("a", "b"), ("b", "a")])
def test_a_cannot_reach_bs_docker_target_or_plan_it_or_bs_release(
    client: TestClient,
    owner_db: Database,
    two_docker_apps: dict[str, DockerSide],
    me: str,
    other: str,
) -> None:
    mine, theirs = two_docker_apps[me], two_docker_apps[other]
    flow, release = mine.flow, str(mine.release.id)
    h = as_user(client, flow.requester)
    request_plan(client, h, flow.slug, mine.target, release, application_key="own")  # own first
    before = _both(owner_db, two_docker_apps)
    nobody, T = str(uuid7()), targets(flow.slug)
    for method, suffix in (("GET", ""), ("POST", "/archive")):
        _same_404(
            client.request(method, f"{T}/{theirs.target}{suffix}", headers=h),
            client.request(method, f"{T}/{nobody}{suffix}", headers=h),
        )
    for named, unknown in (
        (_plan_body(theirs.target, release), _plan_body(nobody, release)),
        (_plan_body(theirs.target, str(theirs.release.id)), _plan_body(nobody, release)),
        (_plan_body(mine.target, str(theirs.release.id)), _plan_body(mine.target, nobody)),
    ):
        _same_404(
            client.post(plans(flow.slug), json=named, headers=h),
            client.post(plans(flow.slug), json=unknown, headers=h),
        )
    assert _both(owner_db, two_docker_apps) == before


@pytest.mark.parametrize(("me", "other"), [("a", "b"), ("b", "a")])
def test_a_cannot_reach_bs_docker_plan_or_deployment(
    client: TestClient,
    owner_db: Database,
    two_docker_apps: dict[str, DockerSide],
    me: str,
    other: str,
) -> None:
    mine, theirs = two_docker_apps[me], two_docker_apps[other]
    slug = mine.flow.slug
    h = as_user(client, mine.flow.requester)
    for url in (f"{plans(slug)}/{mine.sealed['id']}", f"{deployments(slug)}/{mine.deployment}"):
        assert client.get(url, headers=h).status_code == 200  # own objects first
    before = _both(owner_db, two_docker_apps)
    nobody = str(uuid7())
    calls: tuple[tuple[str, str, str, Any], ...] = (
        (plans(slug), theirs.sealed["id"], "", None),
        (plans(slug), theirs.sealed["id"], "/approve", {"plan_digest": theirs.sealed["digest"]}),
        (plans(slug), theirs.sealed["id"], "/reject", {}),
        (deployments(slug), theirs.deployment, "", None),
        (deployments(slug), theirs.deployment, "/reconcile", None),
        (deployments(slug), theirs.deployment, "/mark-failed", {"note": "intruder"}),
        (deployments(slug), theirs.deployment, "/cancel", None),
    )
    for base, their_id, suffix, payload in calls:
        method = "POST" if suffix else "GET"
        _same_404(
            client.request(method, f"{base}/{their_id}{suffix}", headers=h, json=payload),
            client.request(method, f"{base}/{nobody}{suffix}", headers=h, json=payload),
        )
    assert _both(owner_db, two_docker_apps) == before


def test_a_p1_only_member_cannot_reach_p2s_docker_objects(
    client: TestClient, world: World, people: People, owner_db: Database
) -> None:
    """Within one organization: a member bound only to P1, with every delivery permission there,
    gets on P2's Docker target, plan, deployment and release the 404 an unknown id gets."""
    a = world.a
    flow = Flow(
        client,
        a.slug,
        ws_slug(a),
        a.tenant,
        people.owner_a,
        system_role_user(owner_db, a.tenant, "tenant_administrator"),
        make_executor(client, a, people.owner_a).token,
        owner_db,
    )
    host = workspace(flow)
    p1 = docker_target(flow, workspace_host=host, project_scope=project_slug(a))
    release = docker_release(flow, p1)  # in a project of its own: P2
    with owner_db.session() as o:
        p2_slug = o.execute(
            text(
                "SELECT p.slug FROM shifde.projects p JOIN shifde.solutions s "
                "ON s.project_id = p.id WHERE s.id = :s"
            ),
            {"s": release.release.manifest["solution_id"]},
        ).scalar_one()
    p2 = docker_target(flow, workspace_host=host, project_scope=p2_slug)
    assert (p1["project"], p2["project"]) == (project_slug(a), p2_slug)
    approved = flow.approve(flow.sealed(p2["id"], "live-app", release_id=release.id))
    sealed = flow.sealed(p2["id"], release_id=release.id)
    email = member(
        owner_db,
        a.tenant,
        [
            "workspace:read",
            "project:read",
            "packaging:solution:read",
            "delivery:target:read",
            "delivery:target:write",
            "delivery:plan:read",
            "delivery:plan:request",
            "delivery:plan:approve",
            "delivery:deployment:read",
            "delivery:deployment:resolve",
        ],
        scope_type="project",
        scope_id=a.project,
    )
    h = as_user(client, email)
    assert client.get(f"{targets(a.slug)}/{p1['id']}", headers=h).status_code == 200
    before = rows(owner_db, a.tenant)
    nobody, D = str(uuid7()), deployments(a.slug)
    plan, T, P = sealed["id"], targets(a.slug), plans(a.slug)
    deployment = approved["deployment_id"]
    calls: tuple[tuple[str, str, Any], ...] = (
        ("GET", f"{T}/{{}}", p2["id"]),
        ("POST", f"{T}/{{}}/archive", p2["id"]),
        ("GET", f"{P}/{{}}", plan),
        ("POST", f"{P}/{{}}/reject", plan),
        ("GET", f"{D}/{{}}", deployment),
        ("POST", f"{D}/{{}}/reconcile", deployment),
        ("POST", f"{D}/{{}}/cancel", deployment),
    )
    for method, url, their_id in calls:
        sent = {} if url.endswith("/reject") else None
        _same_404(
            client.request(method, url.format(their_id), headers=h, json=sent),
            client.request(method, url.format(nobody), headers=h, json=sent),
        )
    digest = {"plan_digest": sealed["digest"]}
    _same_404(
        client.post(f"{P}/{plan}/approve", headers=h, json=digest),
        client.post(f"{P}/{nobody}/approve", headers=h, json=digest),
    )
    for named, unknown in (
        (_plan_body(p2["id"], str(release.id)), _plan_body(nobody, str(release.id))),
        (_plan_body(p1["id"], str(release.id)), _plan_body(p1["id"], nobody)),
    ):
        _same_404(client.post(P, json=named, headers=h), client.post(P, json=unknown, headers=h))
    assert rows(owner_db, a.tenant) == before


def _docker_body(flow: Flow, host: str) -> dict[str, Any]:
    bindings = {**DOCKER_BINDINGS, "workspace_host": host}
    return sp8_body("docker", flow.workspace, sp8_address("docker"), bindings=bindings)


@pytest.mark.parametrize(("me", "other"), [("a", "b"), ("b", "a")])
def test_a_workspace_another_organization_claims_answers_as_an_unclaimed_one(
    client: TestClient,
    owner_db: Database,
    two_docker_apps: dict[str, DockerSide],
    me: str,
    other: str,
) -> None:
    mine, theirs = two_docker_apps[me], two_docker_apps[other]
    flow, release = mine.flow, str(mine.release.id)
    theirs_before = rows(owner_db, theirs.flow.tenant)
    h = as_user(client, flow.requester)  # one sign-in: the per-address login limit
    released = register(client, h, flow.slug, target_body(flow.workspace, address()))
    assert client.post(f"{targets(flow.slug)}/{released['id']}/archive", headers=h).is_success
    answers = []
    for host in (theirs.workspace["address"], address(), released["address"]):
        target = register(client, h, flow.slug, _docker_body(flow, host))["id"]
        before = rows(owner_db, flow.tenant)
        body = request_plan(client, h, flow.slug, target, release, status=409)
        assert body["code"] == "workspace_not_claimed"
        assert rows(owner_db, flow.tenant) == before  # no artifact, plan or job row
        answers.append({k: v for k, v in body.items() if k not in _VARYING})
    assert answers[0] == answers[1] == answers[2]
    assert rows(owner_db, theirs.flow.tenant) == theirs_before
    # the control: this organization's own claimed workspace
    own = register(client, h, flow.slug, _docker_body(flow, mine.workspace["address"]))
    request_plan(client, h, flow.slug, own["id"], release)


@pytest.mark.parametrize("taken", [False, True], ids=["released", "claimed-by-b"])
def test_approval_refuses_a_workspace_released_or_claimed_by_b_since_the_seal(
    client: TestClient,
    owner_db: Database,
    two_docker_apps: dict[str, DockerSide],
    taken: bool,
) -> None:
    a, b = two_docker_apps["a"], two_docker_apps["b"]
    h = as_user(client, a.flow.requester)
    held = f"{targets(a.flow.slug)}/{a.workspace['id']}"
    assert client.post(f"{held}/archive", headers=h).is_success
    client.cookies.clear()
    if taken:
        assert workspace_target(b.flow, a.workspace["address"])["status"] == "active"
    before = _both(owner_db, two_docker_apps)
    h = as_user(client, a.flow.approver)
    r = client.post(
        f"{plans(a.flow.slug)}/{a.sealed['id']}/approve",
        json={"plan_digest": a.sealed["digest"]},
        headers=h,
    )
    client.cookies.clear()
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "workspace_not_claimed"
    assert _both(owner_db, two_docker_apps) == before  # no approval, deployment or job
    assert plan_row(owner_db, a.sealed["id"])["status"] == "sealed"


def test_holds_claim_in_as_context_is_false_for_bs_claim(
    app_db: Database, owner_db: Database, two_docker_apps: dict[str, DockerSide]
) -> None:
    sides = two_docker_apps
    for me, other in (("a", "b"), ("b", "a")):
        own, theirs = (address_key(sides[who].workspace["address"]) for who in (me, other))
        tenant = sides[me].flow.tenant
        with app_session(app_db, tenant) as s:
            assert repository.holds_claim(s, "databricks", own, tenant_id=tenant)
            assert not repository.holds_claim(s, "databricks", theirs, tenant_id=tenant)
        # the explicit filter too: the schema owner's session sees both organizations' rows
        with owner_db.session() as o:
            assert repository.holds_claim(o, "databricks", own, tenant_id=tenant)
            assert not repository.holds_claim(o, "databricks", theirs, tenant_id=tenant)


def test_bs_executor_leases_none_of_as_docker_jobs(
    client: TestClient, owner_db: Database, two_docker_apps: dict[str, DockerSide]
) -> None:
    a, b = two_docker_apps["a"], two_docker_apps["b"]
    h = as_user(client, a.flow.requester)
    request_plan(client, h, a.flow.slug, a.target, a.release.id, application_key="next")
    client.cookies.clear()
    (b_apply,) = deployment_jobs(owner_db, b.deployment, "apply")
    r = lease(client, b.flow.token)
    assert r.status_code == 200 and r.json()["job"]["id"] == str(b_apply["id"]), r.text
    before = rows(owner_db, a.flow.tenant)
    assert lease(client, b.flow.token).status_code == 204
    assert rows(owner_db, a.flow.tenant) == before
    kinds = set()
    for _ in range(2):
        r = lease(client, a.flow.token)
        assert r.status_code == 200, r.text
        assert r.json()["job"]["target_id"] == a.target
        kinds.add(r.json()["job"]["kind"])
    assert kinds == {"plan", "apply"}
