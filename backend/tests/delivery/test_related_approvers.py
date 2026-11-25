"""Related principals of a plan decision, API key creators included (issue #352, B-4113..B-4115;
migration delivery_0009): the plan records the key it was requested with and the decision the key
it was made with, both the server's; `PlanService._related` refuses every related pair, audited
`denied`, and `shifde.delivery_approvals_guard()` refuses the same pairs by a direct INSERT.
Unrelated pairs decide, another organization's rows change nothing, and a recorded key never
changes. Keys are real `api_keys` rows (`apikeys.mint`); people are `tenant_administrator`s.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any  # Any: JSON bodies, rows, httpx responses
from uuid import UUID

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.errors import AppError
from shifde.core.ids import uuid7
from shifde.core.migrations import alembic_config
from shifde.core.settings import Settings
from shifde.identity import apikeys
from tests._support.tenants import add_tenant, add_workspace
from tests.connections.routes import as_user, system_role_user
from tests.delivery import test_approvals as approvals
from tests.delivery.releases import release_for_target
from tests.delivery.routes import (
    address,
    bearer,
    plan_row,
    plans,
    register,
    request_plan,
    target_body,
)
from tests.delivery.test_approvals import (
    Sealed,
    _approvals,
    _code,
    _decide_as_service_account,
    _denials,
    _expired_plan,
    _requested_by,
    _service_account,
    _user_id,
)
from tests.packaging.test_target_kind_db import _scratch
from tests.tenancy.conftest import World, app_session
from tests.tenancy.routes import People, ws_slug

sealed = approvals.sealed  # the fixture: a sealed plan of A's owner, requested on a session

SCOPES = [
    "workspace:read",
    "project:read",
    "delivery:target:read",
    "delivery:plan:read",
    "delivery:plan:request",
    "delivery:plan:approve",
    "packaging:release:read",
    "packaging:solution:read",
]

# the messages of shifde.delivery_approvals_guard()
SESSION = "a plan is decided only in a session of its client"
NO_PLAN = "plan not found"
SAME = "a plan is decided by a person other than its requester"
OWNED_BY_REQUESTER = "a plan is not decided by a service account its requester owns"
OWNER = "a plan is not decided by the owner of the requesting service account"
SIBLING = "a plan is not decided by a service account with the same owner"
NO_REQUEST_KEY = "the API key of the plan request is not found"
KEY_CREATOR = "a plan is not decided by the creator of the requesting API key"
OWNED_BY_KEY_CREATOR = (
    "a plan is not decided by a service account the creator of the requesting API key owns"
)
NO_DECIDING_KEY = "the API key of the decision is not found"
KEY_OF_REQUESTER = "a plan is not decided with an API key its requester created"
KEY_OF_OWNER = (
    "a plan is not decided with an API key the owner of the requesting service account created"
)
NOT_SEALED = "the plan is not sealed, its digest differs or it has expired"


@dataclass(frozen=True)
class Key:
    id: UUID
    secret: str


@dataclass(frozen=True)
class Decider:
    id: UUID
    email: str | None = None  # a person on a session
    key: Key | None = None  # the key the decider uses instead

    @property
    def key_id(self) -> UUID | None:
        return self.key.id if self.key else None


@dataclass(frozen=True)
class Lab:
    """Organization A with a sealed plan, and what makes accounts, keys and plans in it."""

    client: TestClient
    world: World
    owner_db: Database
    app_db: Database
    settings: Settings
    sealed: Sealed

    @property
    def tenant(self) -> UUID:
        return self.world.a.tenant

    @property
    def digest(self) -> str:
        return str(self.sealed.plan["digest"])

    def person(self) -> tuple[str, UUID]:
        email = system_role_user(self.owner_db, self.tenant, "tenant_administrator")
        return email, _user_id(self.owner_db, email)

    def account(self, owner: UUID | None = None) -> UUID:
        return _service_account(self.owner_db, self.tenant, owner)

    def key(
        self,
        principal: UUID,
        created_by: UUID | None,
        *,
        kind: str = "service_account",
        tenant: UUID | None = None,
    ) -> Key:
        tenant = tenant or self.tenant
        with app_session(self.app_db, tenant, created_by) as s:
            row, secret = apikeys.mint(
                s,
                self.settings,
                tenant_id=tenant,
                name=f"key-{uuid7().hex[-12:]}",
                principal_type=kind,
                principal_id=principal,
                scopes=SCOPES,
                resource_scope=None,
                expires_at=utcnow() + timedelta(days=1),
                created_by=created_by,
            )
            key = Key(row.id, secret)
            s.commit()
        return key

    def plan(self, requester: UUID, key: UUID | None = None) -> UUID:
        """A sealed plan `requester` requested with `key`."""
        return _requested_by(self.owner_db, self.sealed.plan, requester, key)

    def decide(self, plan_id: object, action: str, by: Decider) -> Any:  # Any: a response
        body = {"plan_digest": self.digest} if action == "approve" else {}
        self.client.cookies.clear()
        headers = bearer(by.key.secret) if by.key else as_user(self.client, str(by.email))
        url = f"{plans(self.world.a.slug)}/{plan_id}/{action}"
        r = self.client.post(url, json=body, headers=headers)
        self.client.cookies.clear()
        return r

    def insert(
        self,
        plan_id: object,
        decided_by: UUID,
        key: UUID | None,
        decision: str = "approve",
        *,
        digest: str | None = None,
        context: UUID | None = None,
        no_client: bool = False,
    ) -> None:
        """A direct INSERT as `shifde_app` of a decision on A's plan, in A's context unless
        another organization's (`context`) or none (`no_client`) is asked."""
        with app_session(self.app_db, None if no_client else context or self.tenant) as s:
            s.execute(
                text(
                    "INSERT INTO shifde.delivery_approvals (tenant_id, id, plan_id, plan_digest, "
                    "decided_by, decided_api_key_id, decision) VALUES (:t, :i, :p, :d, :u, :k, :x)"
                ),
                {
                    "t": self.tenant,
                    "i": uuid7(),
                    "p": plan_id,
                    "d": digest or self.digest,
                    "u": decided_by,
                    "k": key,
                    "x": decision,
                },
            )
            s.commit()


@pytest.fixture
def lab(
    client: TestClient,
    world: World,
    owner_db: Database,
    app_db: Database,
    settings: Settings,
    sealed: Sealed,
) -> Lab:
    return Lab(client, world, owner_db, app_db, settings, sealed)


Pair = tuple[UUID, Decider]  # the plan and who tries to decide it


def _state(exc: pytest.ExceptionInfo[DBAPIError]) -> tuple[str | None, str | None]:
    """(SQLSTATE, message)."""
    diag = getattr(exc.value.orig, "diag", None)
    return getattr(exc.value.orig, "sqlstate", None), getattr(diag, "message_primary", None)


# ------------------------------------------------------------------ related pairs
def _same_principal(lab: Lab) -> Pair:
    """... which also made the key the plan was requested with."""
    sa = lab.account()
    key = lab.key(sa, sa)
    return lab.plan(sa, key.id), Decider(sa, key=key)


def _owned_by_requester(lab: Lab) -> Pair:
    """... who also created its key."""
    _, p = lab.person()
    sa = lab.account(p)
    return lab.plan(p), Decider(sa, key=lab.key(sa, p))


def _owner_of_requester(lab: Lab) -> Pair:
    """... who also created the requesting key."""
    email, p = lab.person()
    sa = lab.account(p)
    return lab.plan(sa, lab.key(sa, p).id), Decider(p, email)


def _siblings(lab: Lab) -> Pair:
    """... whose owner also created both keys."""
    _, p = lab.person()
    first, second = lab.account(p), lab.account(p)
    return lab.plan(first, lab.key(first, p).id), Decider(second, key=lab.key(second, p))


def _creator_on_a_session(lab: Lab) -> Pair:
    email, p = lab.person()
    sa = lab.account()
    return lab.plan(sa, lab.key(sa, p).id), Decider(p, email)


def _creator_with_a_personal_key(lab: Lab) -> Pair:
    email, p = lab.person()
    sa = lab.account()
    personal = lab.key(p, p, kind="user")
    return lab.plan(sa, lab.key(sa, p).id), Decider(p, email, personal)


def _account_that_created_the_key(lab: Lab) -> Pair:
    _, other = lab.person()
    sa, creator = lab.account(), lab.account()
    key = lab.key(sa, creator)
    return lab.plan(sa, key.id), Decider(creator, key=lab.key(creator, other))


def _owned_by_the_key_creator(lab: Lab) -> Pair:
    (_, p), (_, other) = lab.person(), lab.person()
    sa, decider = lab.account(), lab.account(p)
    return lab.plan(sa, lab.key(sa, p).id), Decider(decider, key=lab.key(decider, other))


def _key_the_requester_created(lab: Lab) -> Pair:
    _, p = lab.person()
    sa = lab.account()
    return lab.plan(p), Decider(sa, key=lab.key(sa, p))


def _key_the_requesting_account_created(lab: Lab) -> Pair:
    first, second = lab.account(), lab.account()
    return lab.plan(first), Decider(second, key=lab.key(second, first))


def _key_the_requesters_owner_created(lab: Lab) -> Pair:
    (_, p), (_, other) = lab.person(), lab.person()
    first, second = lab.account(p), lab.account(other)
    return lab.plan(first), Decider(second, key=lab.key(second, p))


Related = tuple[Callable[[Lab], Pair], str, str]
EXISTING: list[Related] = [
    (_same_principal, "same_principal", SAME),
    (_owned_by_requester, "decider_owned_by_requester", OWNED_BY_REQUESTER),
    (_owner_of_requester, "requester_owned_by_decider", OWNER),
    (_siblings, "sibling_service_accounts", SIBLING),
]
NEW: list[Related] = [
    (_creator_on_a_session, "decider_created_requesting_key", KEY_CREATOR),
    (_creator_with_a_personal_key, "decider_created_requesting_key", KEY_CREATOR),
    (_account_that_created_the_key, "decider_created_requesting_key", KEY_CREATOR),
    (_owned_by_the_key_creator, "decider_owned_by_requesting_key_creator", OWNED_BY_KEY_CREATOR),
    (_key_the_requester_created, "decider_key_created_by_requester", KEY_OF_REQUESTER),
    (_key_the_requesting_account_created, "decider_key_created_by_requester", KEY_OF_REQUESTER),
    (_key_the_requesters_owner_created, "decider_key_created_by_requester_owner", KEY_OF_OWNER),
]
RELATED = [
    pytest.param(build, reason, message, id=build.__name__.strip("_"))
    for build, reason, message in EXISTING + NEW
]


def _undecided(lab: Lab, plan_id: object) -> None:
    """No decision, nothing queued by one, and the plan is still `sealed`."""
    assert _approvals(lab.owner_db, plan_id) == []
    assert plan_row(lab.owner_db, plan_id)["status"] == "sealed"
    with lab.owner_db.session() as o:
        queued = o.execute(
            text(
                "SELECT (SELECT count(*) FROM shifde.delivery_deployments WHERE plan_id = :p), "
                "(SELECT count(*) FROM shifde.delivery_jobs WHERE plan_id = :p AND kind = 'apply')"
            ),
            {"p": plan_id},
        ).one()
    assert tuple(queued) == (0, 0)


def _decided(lab: Lab, plan_id: object, by: Decider) -> None:
    """Exactly one approval, by `by`, with the key they used (NULL for a session)."""
    [row] = _approvals(lab.owner_db, plan_id)
    assert (row["decided_by"], row["decided_api_key_id"]) == (by.id, by.key_id)
    assert (row["decision"], row["tenant_id"]) == ("approve", lab.tenant)


@pytest.mark.parametrize(("build", "reason", "message"), RELATED)
def test_a_related_principal_cannot_decide_through_the_service(
    lab: Lab, build: Callable[[Lab], Pair], reason: str, message: str
) -> None:
    """Approve and reject; a pair of an earlier rule that is also one of a new rule reports the
    earlier reason."""
    plan_id, by = build(lab)
    for action in ("approve", "reject"):
        _code(lab.decide(plan_id, action, by), 403, "self_approval_forbidden")
    rows = _denials(lab.owner_db, lab.tenant)
    assert [(r.action, r.outcome, r.target_id, r.actor_id) for r in rows] == [
        ("delivery.plan.approve", "denied", str(plan_id), by.id),
        ("delivery.plan.reject", "denied", str(plan_id), by.id),
    ]
    assert [r.details["reason"] for r in rows] == [reason, reason]
    assert [r.details["decision"] for r in rows] == ["approve", "reject"]
    _undecided(lab, plan_id)


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize(("build", "reason", "message"), RELATED)
def test_a_related_principal_cannot_decide_by_a_direct_insert(
    lab: Lab, build: Callable[[Lab], Pair], reason: str, message: str, decision: str
) -> None:
    plan_id, by = build(lab)
    with pytest.raises(DBAPIError) as exc:
        lab.insert(plan_id, by.id, by.key_id, decision)
    assert _state(exc) == ("42501", message)
    _undecided(lab, plan_id)


# ------------------------------------------------------------------ unresolved keys
@pytest.mark.parametrize("whose", ["unknown", "client_b", "another_principal"])
def test_a_decider_whose_key_is_not_theirs_is_refused(lab: Lab, whose: str) -> None:
    sa = lab.account()
    if whose == "unknown":
        key = uuid7()
    elif whose == "client_b":
        key = lab.key(lab.world.b.service_account, uuid7(), tenant=lab.world.b.tenant).id
    else:
        key = lab.key(lab.account(), uuid7()).id
    plan_id = str(lab.sealed.plan["id"])
    for decision in ("approve", "reject"):
        out = _decide_as_service_account(
            lab.app_db, lab.settings, lab.world, sa, plan_id, decision, lab.digest, key
        )
        assert isinstance(out, AppError), out
        assert (out.status, out.code) == (403, "self_approval_forbidden")
        with pytest.raises(DBAPIError) as exc:
            lab.insert(plan_id, sa, key, decision)
        assert _state(exc) == ("42501", NO_DECIDING_KEY)
    rows = _denials(lab.owner_db, lab.tenant)
    assert [(r.action, r.outcome, r.target_id, r.actor_id) for r in rows] == [
        ("delivery.plan.approve", "denied", plan_id, sa),
        ("delivery.plan.reject", "denied", plan_id, sa),
    ]
    assert [r.details["reason"] for r in rows] == ["api_key_unresolved"] * 2
    assert not [r for r in rows if str(key) in str(r.details)]
    _undecided(lab, plan_id)


def test_a_plan_whose_key_is_not_its_requesters_is_refused(lab: Lab) -> None:
    _, p = lab.person()
    requester, other = lab.account(), lab.account()
    plan_id = lab.plan(requester, lab.key(other, p).id)
    by = Decider(_user_id(lab.owner_db, lab.sealed.approver), lab.sealed.approver)
    for action in ("approve", "reject"):
        _code(lab.decide(plan_id, action, by), 403, "self_approval_forbidden")
        with pytest.raises(DBAPIError) as exc:
            lab.insert(plan_id, by.id, None, action)
        assert _state(exc) == ("42501", NO_REQUEST_KEY)
    rows = _denials(lab.owner_db, lab.tenant)
    assert [r.details["reason"] for r in rows] == ["api_key_unresolved"] * 2
    _undecided(lab, plan_id)


def test_an_unresolved_key_is_reported_before_the_key_creators(lab: Lab) -> None:
    """... and after the earlier rules."""
    _, p = lab.person()
    requester, creator = lab.account(), lab.account()
    created = lab.plan(requester, lab.key(requester, creator).id)
    for decider, plan_id in ((creator, created), (lab.account(p), lab.plan(p))):
        out = _decide_as_service_account(
            lab.app_db, lab.settings, lab.world, decider, plan_id, "approve", lab.digest, uuid7()
        )
        assert isinstance(out, AppError) and out.code == "self_approval_forbidden", out
    assert [r.details["reason"] for r in _denials(lab.owner_db, lab.tenant)] == [
        "api_key_unresolved",
        "decider_owned_by_requester",
    ]


# ------------------------------------------------------------------ the guard's other refusals
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_decision_outside_a_session_of_the_client_is_refused(lab: Lab, decision: str) -> None:
    plan_id = lab.sealed.plan["id"]
    with pytest.raises(DBAPIError) as exc:
        lab.insert(plan_id, uuid7(), None, decision, context=lab.world.b.tenant)
    assert _state(exc) == ("42501", SESSION)
    with pytest.raises(DBAPIError) as exc:
        lab.insert(plan_id, uuid7(), None, decision, no_client=True)
    assert _state(exc) == ("42501", SESSION)
    with pytest.raises(DBAPIError) as exc:
        lab.insert(uuid7(), uuid7(), None, decision)
    assert _state(exc) == ("42501", NO_PLAN)
    _undecided(lab, plan_id)


def test_an_unrelated_decision_still_needs_the_sealed_digest_in_time(
    lab: Lab, people: People
) -> None:
    plan_id = lab.sealed.plan["id"]
    _, other = lab.person()
    decider = lab.account()
    key = lab.key(decider, other).id
    requested = approvals._request(lab.client, lab.world, people.owner_a, lab.owner_db)["id"]
    expired = _expired_plan(lab.owner_db, lab.world, lab.sealed.plan)
    closed = lab.plan(uuid7())  # its digest and its window, but no longer sealed
    with lab.owner_db.session() as o:
        o.execute(
            text("UPDATE shifde.delivery_plans SET status = 'expired' WHERE id = :p"),
            {"p": closed},
        )
        o.commit()
    for plan, digest in (
        (plan_id, "e" * 64),
        (requested, None),
        (expired, None),
        (closed, None),
    ):
        with pytest.raises(DBAPIError) as exc:
            lab.insert(plan, decider, key, digest=digest)
        assert _state(exc) == ("23514", NOT_SEALED)
    lab.insert(plan_id, decider, key)
    with pytest.raises(DBAPIError) as exc:
        lab.insert(plan_id, uuid7(), None, "reject")
    assert _state(exc)[0] == "23505"
    assert len(_approvals(lab.owner_db, plan_id)) == 1


# ------------------------------------------------------------------ unrelated pairs
def _unowned_with_unknown_creators(lab: Lab) -> Pair:
    first, second = lab.account(), lab.account()
    return lab.plan(first, lab.key(first, None).id), Decider(second, key=lab.key(second, None))


def _unowned_with_unrelated_creators(lab: Lab) -> Pair:
    (_, p), (_, q) = lab.person(), lab.person()
    first, second = lab.account(), lab.account()
    return lab.plan(first, lab.key(first, p).id), Decider(second, key=lab.key(second, q))


def _different_owners_and_creators(lab: Lab) -> Pair:
    owners = [lab.person()[1] for _ in range(4)]
    first, second = lab.account(owners[0]), lab.account(owners[1])
    plan_id = lab.plan(first, lab.key(first, owners[2]).id)
    return plan_id, Decider(second, key=lab.key(second, owners[3]))


def _a_person_unrelated_to_the_requesting_key(lab: Lab) -> Pair:
    (email, p), (_, q) = lab.person(), lab.person()
    sa = lab.account(q)
    return lab.plan(sa, lab.key(sa, q).id), Decider(p, email)


def _a_person_with_their_own_key(lab: Lab) -> Pair:
    (email, p), (_, q) = lab.person(), lab.person()
    sa = lab.account()
    personal = lab.key(p, p, kind="user")
    return lab.plan(sa, lab.key(sa, q).id), Decider(p, email, personal)


def _two_people_with_their_own_keys(lab: Lab) -> Pair:
    (email, p), (_, q) = lab.person(), lab.person()
    requesting = lab.key(q, q, kind="user")
    return lab.plan(q, requesting.id), Decider(p, email, lab.key(p, p, kind="user"))


def _one_third_person_created_both_keys(lab: Lab) -> Pair:
    """The accepted limit of B-4115 (issue #395)."""
    (_, p), (_, q), (_, creator) = lab.person(), lab.person(), lab.person()
    first, second = lab.account(p), lab.account(q)
    plan_id = lab.plan(first, lab.key(first, creator).id)
    return plan_id, Decider(second, key=lab.key(second, creator))


UNRELATED = [
    pytest.param(build, id=build.__name__.strip("_"))
    for build in (
        _unowned_with_unknown_creators,
        _unowned_with_unrelated_creators,
        _different_owners_and_creators,
        _a_person_unrelated_to_the_requesting_key,
        _a_person_with_their_own_key,
        _two_people_with_their_own_keys,
        _one_third_person_created_both_keys,
    )
]


@pytest.mark.parametrize("build", UNRELATED)
def test_an_unrelated_principal_approves(lab: Lab, build: Callable[[Lab], Pair]) -> None:
    plan_id, by = build(lab)
    r = lab.decide(plan_id, "approve", by)
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    _decided(lab, plan_id, by)
    assert plan_row(lab.owner_db, plan_id)["status"] == "approved"
    assert _denials(lab.owner_db, lab.tenant) == []


@pytest.mark.parametrize("build", UNRELATED)
def test_an_unrelated_principal_approves_by_a_direct_insert(
    lab: Lab, build: Callable[[Lab], Pair]
) -> None:
    plan_id, by = build(lab)
    lab.insert(plan_id, by.id, by.key_id)
    _decided(lab, plan_id, by)


# ------------------------------------------------------------------ the key is the server's
def test_the_requesting_key_is_the_callers(
    client: TestClient, world: World, people: People, owner_db: Database, lab: Lab
) -> None:
    slug = world.a.slug
    h = as_user(client, people.owner_a)
    target = register(client, h, slug, target_body(ws_slug(world.a), address()))["id"]
    release = release_for_target(owner_db, target).id
    on_session = request_plan(client, h, slug, target, release)
    named = {"requested_api_key_id": str(uuid7())}
    request_plan(client, h, slug, target, release, status=422, **named)
    client.cookies.clear()
    _, creator = lab.person()
    sa = lab.account()
    key = lab.key(sa, creator)
    with_key = request_plan(client, bearer(key.secret), slug, target, release)
    named = {"requested_api_key_id": str(key.id)}
    request_plan(client, bearer(key.secret), slug, target, release, status=422, **named)
    with owner_db.session() as o:
        stored = o.execute(
            text(
                "SELECT id::text, requested_by, requested_api_key_id FROM shifde.delivery_plans "
                "WHERE target_id = :g ORDER BY id"
            ),
            {"g": target},
        ).all()
    assert [tuple(r) for r in stored] == [
        (on_session["id"], _user_id(owner_db, people.owner_a), None),
        (with_key["id"], sa, key.id),
    ]
    read = client.get(f"{plans(slug)}/{with_key['id']}", headers=bearer(key.secret))
    assert read.status_code == 200, read.text
    assert str(key.id) not in read.text and "api_key" not in read.text
    assert str(key.id) not in str(with_key) and "api_key" not in str(with_key)


def test_the_deciding_key_is_the_callers(lab: Lab) -> None:
    (email, p), (_, q) = lab.person(), lab.person()
    plan_id = lab.plan(q)
    url = f"{plans(lab.world.a.slug)}/{plan_id}"
    key = lab.key(p, p, kind="user")
    for headers in (as_user(lab.client, email), bearer(key.secret)):
        for action, body in (("approve", {"plan_digest": lab.digest}), ("reject", {})):
            named = {**body, "decided_api_key_id": str(key.id)}
            r = lab.client.post(f"{url}/{action}", json=named, headers=headers)
            assert r.status_code == 422, r.text
        lab.client.cookies.clear()
    _undecided(lab, plan_id)
    r = lab.client.post(f"{url}/reject", json={}, headers=bearer(key.secret))
    assert r.status_code == 200, r.text
    assert str(key.id) not in r.text and "api_key" not in r.text
    [row] = _approvals(lab.owner_db, plan_id)
    assert (row["decision"], row["decided_api_key_id"]) == ("reject", key.id)


# ------------------------------------------------------------------ another organization
def test_client_b_rows_change_no_decision_in_a(lab: Lab) -> None:
    """B's accounts and keys name A's requester, decider, owners and creators as their owner and
    creator."""
    p, q, r, s = (lab.person()[1] for _ in range(4))
    first, second = lab.account(p), lab.account(q)
    requesting, deciding = lab.key(first, r), lab.key(second, s)
    b = lab.world.b.tenant
    for principal in (first, second, p, q, r, s):
        account = _service_account(lab.owner_db, b, principal)
        for creator in (first, second, p, q, r, s):
            lab.key(account, creator, tenant=b)
    by = Decider(second, key=deciding)
    direct, routed = lab.plan(first, requesting.id), lab.plan(first, requesting.id)
    lab.insert(direct, second, deciding.id)
    _decided(lab, direct, by)
    out = lab.decide(routed, "approve", by)
    assert out.status_code == 200 and out.json()["status"] == "approved", out.text
    _decided(lab, routed, by)
    assert _denials(lab.owner_db, lab.tenant) == []


def test_a_plan_names_only_a_key_of_its_client(lab: Lab) -> None:
    b_key = lab.key(lab.world.b.service_account, uuid7(), tenant=lab.world.b.tenant).id
    for key in (b_key, uuid7()):
        with app_session(lab.app_db, lab.tenant) as s, pytest.raises(DBAPIError) as exc:
            s.execute(
                text(
                    "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, "
                    "target_revision, artifact_id, artifact_digest, application_key, "
                    "display_name, requested_by, requested_api_key_id) SELECT tenant_id, :i, "
                    "target_id, target_revision, artifact_id, artifact_digest, application_key, "
                    "display_name, requested_by, :k FROM shifde.delivery_plans WHERE id = :p"
                ),
                {"i": uuid7(), "k": key, "p": lab.sealed.plan["id"]},
            )
        assert _state(exc)[0] == "23503"
        diag = getattr(exc.value.orig, "diag", None)
        assert getattr(diag, "constraint_name", None) == "fk_delivery_plans__requested_api_key"


# ------------------------------------------------------------------ immutability
def test_a_recorded_key_never_changes(lab: Lab) -> None:
    (_, p), (_, q) = lab.person(), lab.person()
    sa, decider = lab.account(), lab.account()
    key, other, deciding = lab.key(sa, p), lab.key(sa, p), lab.key(decider, q)
    plan_id, without_key = lab.plan(sa, key.id), lab.plan(sa)
    lab.insert(plan_id, decider, deciding.id)
    before = dict(plan_row(lab.owner_db, plan_id))
    for table, row, column in (
        ("delivery_plans", "id", "requested_api_key_id"),
        ("delivery_approvals", "plan_id", "decided_api_key_id"),
    ):
        for value in (other.id, None):
            with app_session(lab.app_db, lab.tenant) as s, pytest.raises(DBAPIError) as exc:
                s.execute(
                    text(f"UPDATE shifde.{table} SET {column} = :k WHERE {row} = :p"),
                    {"k": value, "p": plan_id},
                )
            assert _state(exc) == ("42501", f"permission denied for table {table}")
    for plan, value in ((plan_id, other.id), (plan_id, None), (without_key, key.id)):
        with lab.owner_db.session() as o, pytest.raises(DBAPIError) as exc:
            o.execute(
                text("UPDATE shifde.delivery_plans SET requested_api_key_id = :k WHERE id = :p"),
                {"k": value, "p": plan},
            )
        assert _state(exc) == ("42501", "a sealed plan is immutable")
    assert dict(plan_row(lab.owner_db, plan_id)) == before
    assert plan_row(lab.owner_db, without_key)["requested_api_key_id"] is None
    _decided(lab, plan_id, Decider(decider, key=deciding))


# ------------------------------------------------------------------ migration
_GUARDS = "('delivery_approvals_guard', 'delivery_plans_seal_guard')"
# name, owned by the schema owner, search_path, SECURITY DEFINER, EXECUTE for PUBLIC
_FUNCTIONS = (
    "SELECT p.proname, p.proowner = n.nspowner, p.proconfig, p.prosecdef, p.proacl IS NULL OR "
    "EXISTS (SELECT 1 FROM pg_catalog.aclexplode(p.proacl) a WHERE a.grantee = 0) "
    "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
    f"WHERE n.nspname = 'shifde' AND p.proname IN {_GUARDS} ORDER BY p.proname"
)
FUNCTIONS = [
    (name, True, ["search_path=pg_catalog, pg_temp"], False, False)
    for name in ("delivery_approvals_guard", "delivery_plans_seal_guard")
]
_BODIES = (
    "SELECT p.proname, p.prosrc FROM pg_catalog.pg_proc p "
    "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
    f"WHERE n.nspname = 'shifde' AND p.proname IN {_GUARDS} ORDER BY p.proname"
)
# the two columns, and whether shifde_app may insert and update them
_COLUMNS = (
    "SELECT c.relname, a.attname, "
    "has_column_privilege('shifde_app', c.oid, a.attnum, 'INSERT'), "
    "has_column_privilege('shifde_app', c.oid, a.attnum, 'UPDATE') "
    "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'shifde' AND NOT a.attisdropped "
    "AND c.relname IN ('delivery_plans', 'delivery_approvals') "
    "AND a.attname IN ('requested_api_key_id', 'decided_api_key_id') ORDER BY c.relname"
)
COLUMNS = [
    ("delivery_approvals", "decided_api_key_id", True, False),
    ("delivery_plans", "requested_api_key_id", True, False),
]
_KEYS = (
    "SELECT k.conname, pg_get_constraintdef(k.oid) FROM pg_catalog.pg_constraint k "
    "WHERE k.conname IN ('fk_delivery_plans__requested_api_key', "
    "'fk_delivery_approvals__decided_api_key') ORDER BY k.conname"
)
KEYS = [
    (
        "fk_delivery_approvals__decided_api_key",
        "FOREIGN KEY (tenant_id, decided_api_key_id) REFERENCES shifde.api_keys(tenant_id, id) "
        "ON DELETE RESTRICT",
    ),
    (
        "fk_delivery_plans__requested_api_key",
        "FOREIGN KEY (tenant_id, requested_api_key_id) REFERENCES shifde.api_keys(tenant_id, id) "
        "ON DELETE RESTRICT",
    ),
]


def _migration(revision: str) -> Any:  # Any: the migration module
    script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://unused/x"))
    found = script.get_revision(revision)
    assert found is not None, revision
    return found.module


def _bodies(migration: Any) -> list[tuple[str, str]]:  # Any: a migration module
    """`_BODIES` of the migration's two functions: what is between the dollar quotes."""
    return [
        ("delivery_approvals_guard", migration.APPROVALS_GUARD.split("$$")[1]),
        ("delivery_plans_seal_guard", migration.SEAL_GUARD.split("$$")[1]),
    ]


def test_the_previous_bodies_are_delivery_0004s() -> None:
    old, new = _migration("delivery_0004"), _migration("delivery_0009")
    for name in ("SEAL_GUARD", "APPROVALS_GUARD"):
        assert getattr(new, f"PREVIOUS_{name}") == getattr(old, name).replace(
            "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
        )
    # the new seal guard is that body and the one column, on both sides
    seal = new.SEAL_GUARD
    for side in ("NEW", "OLD"):
        line = f"            {side}.requested_api_key_id,\n"
        assert seal.count(f"{side}.requested_by,\n{line}") == 1
        seal = seal.replace(line, "")
    assert seal == new.PREVIOUS_SEAL_GUARD


def _client_plan(c: Connection) -> dict[str, Any]:
    """A new client's approved sealed plan, in the client's context (owner rights), naming no
    column of delivery_0009."""
    tenant = add_tenant(c)
    ws = add_workspace(c, tenant)
    c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
    ids = {"t": tenant, "w": ws, "i": uuid7(), "u": uuid7(), "v": uuid7()}
    for sql in (
        "INSERT INTO shifde.delivery_targets (id, tenant_id, workspace_id, name, platform_kind, "
        "address, claim_id, environment, bindings, credential_ref, status, created_by) VALUES "
        "(:i, :t, :w, 'x', 'databricks', 'https://x', NULL, 'dev', '{}', 'ref', 'archived', :u)",
        "INSERT INTO shifde.delivery_artifacts (tenant_id, id, target_kind, digest, size, "
        "content, created_by) VALUES (:t, :i, 'databricks.app/v1', repeat('e', 64), 1, 'x', :u)",
        "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, target_revision, "
        "artifact_id, artifact_digest, application_key, display_name, status, requested_by, "
        "steps, snapshot, adapter_version, toolchain_digest, digest, sealed_at, expires_at) "
        "VALUES (:t, :i, :i, 1, :i, repeat('e', 64), 'sales-app', 'Sales', 'sealed', :u, '[]', "
        "'{}', '1', repeat('c', 64), repeat('d', 64), now(), now() + interval '15 minutes')",
        "INSERT INTO shifde.delivery_approvals (tenant_id, id, plan_id, plan_digest, decided_by, "
        "decision) VALUES (:t, :i, :i, repeat('d', 64), :v, 'approve')",
    ):
        c.execute(text(sql), ids)
    return ids


def _client_plan_with_keys(c: Connection) -> None:
    """`_client_plan` of another plan id, both new columns set (the requester's and the
    decider's key)."""
    ids = _client_plan(c) | {"p": uuid7(), "k": uuid7(), "l": uuid7()}
    for sql in (
        "INSERT INTO shifde.api_keys (id, tenant_id, name, principal_type, principal_id, prefix, "
        "key_hash, scopes, expires_at) VALUES "
        "(:k, :t, 'k', 'service_account', :u, left(md5(:pk), 8), 'x', '{}', now()), "
        "(:l, :t, 'l', 'service_account', :v, left(md5(:pl), 8), 'x', '{}', now())",
        "INSERT INTO shifde.delivery_plans (tenant_id, id, target_id, target_revision, "
        "artifact_id, artifact_digest, application_key, display_name, status, requested_by, "
        "steps, snapshot, adapter_version, toolchain_digest, digest, sealed_at, expires_at, "
        "requested_api_key_id) SELECT tenant_id, :p, target_id, target_revision, artifact_id, "
        "artifact_digest, application_key, display_name, status, requested_by, steps, snapshot, "
        "adapter_version, toolchain_digest, digest, sealed_at, expires_at, :k "
        "FROM shifde.delivery_plans WHERE id = :i",
        "INSERT INTO shifde.delivery_approvals (tenant_id, id, plan_id, plan_digest, decided_by, "
        "decided_api_key_id, decision) VALUES (:t, :p, :p, repeat('d', 64), :v, :l, 'approve')",
    ):
        c.execute(text(sql), {**ids, "pk": ids["k"].hex, "pl": ids["l"].hex})


def test_the_downgrade_and_upgrade_restore_the_guards_and_keep_rows(
    run_id: str, admin_url: str, owner_url: str, app_url: str
) -> None:
    old, new = _migration("delivery_0004"), _migration("delivery_0009")
    with _scratch(run_id, admin_url, owner_url, app_url, "dlvrel") as url:
        cfg = alembic_config(url)
        engine = create_engine(url, poolclass=NullPool)

        def catalog(sql: str) -> list[tuple[Any, ...]]:
            with engine.connect() as c:
                return [tuple(r) for r in c.execute(text(sql))]

        def at_0009() -> None:
            assert catalog(_BODIES) == _bodies(new)
            assert catalog(_FUNCTIONS) == FUNCTIONS
            assert catalog(_KEYS) == KEYS
            assert catalog(_COLUMNS) == COLUMNS

        try:
            at_0009()
            with engine.begin() as c:  # recorded keys do not refuse the downgrade
                _client_plan_with_keys(c)
            command.downgrade(cfg, "delivery_0008")
            assert catalog(_BODIES) == _bodies(old)
            assert catalog(_FUNCTIONS) == FUNCTIONS
            assert catalog(_KEYS) == [] and catalog(_COLUMNS) == []
            with engine.begin() as c:
                ids = _client_plan(c)
            command.upgrade(cfg, "heads")
            at_0009()
            with engine.begin() as c:
                c.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(ids["t"])}
                )
                kept = c.execute(
                    text(
                        "SELECT p.status, p.requested_by, p.requested_api_key_id, a.decision, "
                        "a.decided_by, a.decided_api_key_id FROM shifde.delivery_plans p "
                        "JOIN shifde.delivery_approvals a ON a.plan_id = p.id WHERE p.id = :i"
                    ),
                    ids,
                ).one()
            assert tuple(kept) == ("sealed", ids["u"], None, "approve", ids["v"], None)
        finally:
            engine.dispose()
