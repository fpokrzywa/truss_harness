"""Entra sign-in through an installation IdP with the `entra` profile (SP3-6, issue #83; design
SP3 section 8 and section 12 items 23-28; DECISIONS B-2711).

Every case runs the real `/api/auth/oidc/{id}/login` and `/callback` routes against the fake
Entra issuer (`FakeOidcProvider.entra`, `tests/_support/fake_oidc.py`) over the admin app
(`tests/admin/conftest.py::admin_client`, as `shifde_app`): the identity is keyed by `oid`, never
the pairwise `sub`; `tid`, `ver` and `oid` are checked after the ID token is valid; group overage
refuses the sign-in (one `identity.oidc.groups_overage` row, its own redirect, memberships and
sessions unchanged); platform groups are mapped by object id and matched lowercase; and an Entra
user synced into a group bound only in client A reads A and gets 404 on B. An e-mail is verified
only for a member (`email` with `xms_edov` true, else `preferred_username`), never for a guest,
so an unverified address never links a pending user of another client (B-2712). The default fake
(`https://idp.test`) is covered unchanged by `test_oidc*.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JWT claims
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.settings import Settings
from tests._support.fake_oidc import OVERAGE_CLAIM_NAMES, FakeOidcProvider, resolver
from tests._support.tenants import slug_of
from tests.admin.conftest import admin_client
from tests.identity import harness, oidc_flow
from tests.identity.conftest import World, owner_membership, owner_user
from tests.identity.test_oidc_hardening import _arm_link
from tests.identity.test_platform_groups import _bind_group, admin_session

TENANT = "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"
OTHER_TENANT = "0b1c2d3e-4f50-4617-a8b9-cadbecfd0e1f"
OVERAGE = "/login?error=oidc_groups_overage"


@contextmanager
def entra_client(settings: Settings, fake: FakeOidcProvider) -> Iterator[TestClient]:
    s = settings.model_copy(update={"oidc_jwks_min_refresh_seconds": 0})
    with admin_client(s, http_transport=fake.transport(), resolver=resolver) as c:
        yield c


def _fake() -> FakeOidcProvider:
    return FakeOidcProvider.entra(TENANT, require_secret=False)


def sign_in(
    c: TestClient, fake: FakeOidcProvider, idp: UUID, code: str, *, oid: str, **claims: Any
) -> httpx.Response:
    c.cookies.clear()
    auth = oidc_flow.start(c, fake, idp)
    fake.issue_code(auth, fake.entra_claims_for(auth, oid=oid, **claims), code=code)
    return oidc_flow.finish(c, idp, code, auth.state)


def _has_session(r: httpx.Response) -> bool:
    return any(h.startswith(harness.COOKIE) for h in r.headers.get_list("set-cookie"))


def _identities(owner: Session, idp: UUID) -> list[tuple[UUID, str]]:
    rows = owner.execute(
        text("SELECT user_id, subject FROM shifde.user_identities WHERE idp_config_id = :i"),
        {"i": idp},
    ).all()
    owner.rollback()
    return [(r[0], r[1]) for r in rows]


def _audit(owner: Session, action: str) -> list[tuple[str, UUID | None]]:
    rows = owner.execute(
        text(
            "SELECT details->>'reason', tenant_id FROM shifde.audit_events "
            "WHERE action = :a ORDER BY id"
        ),
        {"a": action},
    ).all()
    owner.rollback()
    return [(r[0], r[1]) for r in rows]


def _synced(owner: Session, user: UUID) -> list[str]:
    rows = owner.execute(
        text(
            "SELECT g.external_id FROM shifde.group_members gm JOIN shifde.groups g "
            "ON g.id = gm.group_id WHERE gm.user_id = :u AND gm.source = 'idp_sync' ORDER BY 1"
        ),
        {"u": user},
    ).scalars()
    result = list(rows)
    owner.rollback()
    return result


def _platform_group(c: TestClient, owner: Session, idp: UUID, external_id: str) -> UUID:
    """A platform group mapped to `idp` through `POST /platform/groups`, as the administrator."""
    admin = f"platform-{uuid4().hex[:8]}@shi.test"
    harness.person(owner, admin, admin=True)
    c.cookies.clear()
    headers = harness.login(c, admin)
    made = c.post(
        "/api/v1/platform/groups",
        json={
            "slug": f"entra-{uuid4().hex[:8]}",
            "name": "Entra readers",
            "idp_config_id": str(idp),
            "external_id": external_id,
        },
        headers=headers,
    )
    assert made.status_code == 201, made.text
    c.cookies.clear()
    return UUID(made.json()["id"])


# ------------------------------------------------------------------ identity by oid
def test_jit_sign_in_keys_the_identity_by_oid_and_ignores_sub(
    settings: Settings, world: World, owner_session: Session
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    owner_session.commit()
    fake = _fake()
    oid = str(uuid4())
    with entra_client(settings, fake) as c:
        first = sign_in(c, fake, idp, "c1", oid=oid.upper(), sub="pairwise-one", xms_edov=True)
        assert first.status_code == 302 and first.headers["location"] == "/", first.headers
        assert _has_session(first)
        [(user, subject)] = _identities(owner_session, idp)
        assert subject == oid and subject != "pairwise-one"  # canonical lowercase oid
        second = sign_in(c, fake, idp, "c2", oid=oid, sub="pairwise-two")
        assert second.headers["location"] == "/" and _has_session(second)
    assert _identities(owner_session, idp) == [(user, oid)]  # same person, no second identity
    email = owner_session.execute(
        text("SELECT primary_email FROM shifde.users WHERE id = :u"), {"u": user}
    ).scalar_one()
    assert email == "newuser@example.com"  # `email` with `xms_edov`, without `email_verified`


def test_preferred_username_is_the_address_when_email_is_missing(
    settings: Settings, world: World, owner_session: Session
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    owner_session.commit()
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(
            c, fake, idp, "c1", oid=str(uuid4()), email=None, preferred_username="Ana@Contoso.test"
        )
        assert r.headers["location"] == "/" and _has_session(r)
    [(user, _)] = _identities(owner_session, idp)
    email = owner_session.execute(
        text("SELECT primary_email FROM shifde.users WHERE id = :u"), {"u": user}
    ).scalar_one()
    assert email == "ana@contoso.test"


# ------------------------------------------------------------------ tid, ver and oid
@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"tid": OTHER_TENANT}, "entra_tenant_mismatch"),
        ({"tid": "not-a-guid"}, "entra_tenant_mismatch"),
        ({"tid": None}, "entra_tenant_mismatch"),
        ({"ver": "1.0"}, "entra_version_invalid"),
        ({"ver": None}, "entra_version_invalid"),
        ({"oid": None}, "entra_oid_invalid"),
        ({"oid": "not-a-guid"}, "entra_oid_invalid"),
    ],
)
def test_a_token_with_a_wrong_tid_ver_or_oid_is_refused_and_audited(
    settings: Settings,
    world: World,
    owner_session: Session,
    claims: dict[str, Any],
    reason: str,
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    owner_session.commit()
    fake = _fake()
    claims = dict(claims)
    oid = claims.pop("oid", str(uuid4()))
    with entra_client(settings, fake) as c:
        auth = oidc_flow.start(c, fake, idp)
        token = fake.entra_claims_for(auth, oid=oid, **claims)
        fake.issue_code(auth, token)
        r = oidc_flow.finish(c, idp, "c1", auth.state)
    assert r.status_code == 302 and r.headers["location"] == oidc_flow.FAIL
    assert not _has_session(r)
    assert _audit(owner_session, "identity.login.failed") == [(reason, None)]  # installation
    assert _identities(owner_session, idp) == []


# ------------------------------------------------------------------ group overage
@pytest.mark.parametrize(
    "overage",
    [
        {**OVERAGE_CLAIM_NAMES, "groups": None},
        {"hasgroups": True, "groups": None},
        {"groups": [str(uuid4()) for _ in range(201)]},
    ],
    ids=["claim_names", "hasgroups", "over_200"],
)
def test_group_overage_refuses_the_sign_in_and_keeps_memberships(
    settings: Settings,
    world: World,
    owner_session: Session,
    overage: dict[str, Any],
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT, group_sync_mode="sync")
    owner_session.commit()
    fake = _fake()
    oid, group = str(uuid4()), str(uuid4())
    with entra_client(settings, fake) as c:
        _platform_group(c, owner_session, idp, group)
        ok = sign_in(c, fake, idp, "c1", oid=oid, groups=[group])
        assert ok.headers["location"] == "/"
        [(user, _)] = _identities(owner_session, idp)
        assert _synced(owner_session, user) == [group]
        sessions = owner_session.execute(
            text("SELECT count(*) FROM shifde.sessions WHERE user_id = :u"), {"u": user}
        ).scalar_one()
        owner_session.rollback()
        r = sign_in(c, fake, idp, "c2", oid=oid, **overage)
    assert r.status_code == 302 and r.headers["location"] == OVERAGE
    assert not _has_session(r)
    assert _synced(owner_session, user) == [group]  # unchanged: never a partial list
    after = owner_session.execute(
        text("SELECT count(*) FROM shifde.sessions WHERE user_id = :u"), {"u": user}
    ).scalar_one()
    assert after == sessions
    assert _audit(owner_session, "identity.oidc.groups_overage") == [("groups_overage", None)]
    assert _audit(owner_session, "identity.login.failed") == []  # one row, not two (B-2711)


def test_overage_of_a_new_person_creates_nobody(
    settings: Settings, world: World, owner_session: Session
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT, group_sync_mode="sync")
    owner_session.commit()
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(c, fake, idp, "c1", oid=str(uuid4()), hasgroups=True, groups=None)
    assert r.headers["location"] == OVERAGE and not _has_session(r)
    assert _identities(owner_session, idp) == []


def test_overage_is_not_checked_when_group_sync_is_off(
    settings: Settings, world: World, owner_session: Session
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    owner_session.commit()
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(c, fake, idp, "c1", oid=str(uuid4()), hasgroups=True, groups=None)
    assert r.headers["location"] == "/" and _has_session(r)


# ------------------------------------------------------------------ groups by object id
def test_group_sync_matches_object_ids_whatever_their_case(
    settings: Settings, world: World, owner_session: Session
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT, group_sync_mode="sync")
    owner_session.commit()
    fake = _fake()
    oid, group = str(uuid4()), str(uuid4())
    with entra_client(settings, fake) as c:
        _platform_group(c, owner_session, idp, group.upper())  # stored lowercase
        r = sign_in(c, fake, idp, "c1", oid=oid, groups=[group])
        assert r.headers["location"] == "/"
        [(user, _)] = _identities(owner_session, idp)
        assert _synced(owner_session, user) == [group]
        r = sign_in(c, fake, idp, "c2", oid=oid, groups=[group.upper()])
        assert r.headers["location"] == "/"
        assert _synced(owner_session, user) == [group]
        r = sign_in(c, fake, idp, "c3", oid=oid, groups=[str(uuid4())])
        assert r.headers["location"] == "/"
    assert _synced(owner_session, user) == []  # gone from the claim: removed


# ------------------------------------------------------------------ cross-client
def test_an_entra_user_synced_into_a_group_bound_in_a_reads_a_and_not_b(
    settings: Settings, world: World, app_db: Database, owner_session: Session
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT, group_sync_mode="sync")
    owner_session.commit()
    fake = _fake()
    group_oid = str(uuid4())
    with entra_client(settings, fake) as c:
        group = _platform_group(c, owner_session, idp, group_oid)
        with admin_session(app_db, world.a.tenant) as s:
            _bind_group(s, world.a.tenant, group)
            s.commit()
        r = sign_in(c, fake, idp, "c1", oid=str(uuid4()), groups=[group_oid])
        assert r.headers["location"] == "/" and _has_session(r)
        a = c.get(f"/api/v1/orgs/{slug_of(world.a.tenant)}")
        b = c.get(f"/api/v1/orgs/{slug_of(world.b.tenant)}")
        missing = c.get(f"/api/v1/orgs/{slug_of(uuid4())}")
    assert a.status_code == 200, a.text
    assert b.status_code == missing.status_code == 404
    assert b.json()["detail"] == missing.json()["detail"]


# ------------------------------------------------------------------ e-mail trust (B-2712)
def _pending_victim(owner: Session, world: World) -> tuple[UUID, str]:
    """A person of client B only, with a link armed in B: an installation IdP may complete it
    for whoever presents a verified e-mail of theirs."""
    email = f"victim-{uuid4().hex[:8]}@b.test"
    victim = owner_user(owner, email)
    owner_membership(owner, world.b.tenant, victim)
    _arm_link(owner, victim, world.b.tenant)
    owner.commit()
    return victim, email


def _link_pending(owner: Session, user: UUID) -> bool:
    pending = owner.execute(
        text(
            "SELECT count(*) FROM shifde.setup_tokens WHERE user_id = :u "
            "AND link_pending_until IS NOT NULL"
        ),
        {"u": user},
    ).scalar_one()
    owner.rollback()
    return bool(pending)


def _user_count(owner: Session) -> int:
    count = owner.execute(text("SELECT count(*) FROM shifde.users")).scalar_one()
    owner.rollback()
    return int(count)


@pytest.mark.parametrize(
    "guest",
    [
        {"acct": 1},
        {"acct": "1"},
        {"acct": 0, "preferred_username": "victim_b.test#EXT#@contoso.onmicrosoft.com"},
        {"acct": 0, "upn": "victim_b.test#ext#@contoso.onmicrosoft.com"},
    ],
    ids=["acct_1", "acct_str", "ext_username", "ext_upn"],
)
def test_a_guest_email_never_links_a_pending_user_of_another_client(
    settings: Settings, world: World, owner_session: Session, guest: dict[str, Any]
) -> None:
    """Isolation review 036-1 item 1: a guest of the pinned directory presenting client B's
    person's address (even with `xms_edov` true) is neither linked to them nor provisioned."""
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    victim, email = _pending_victim(owner_session, world)
    owner_session.commit()
    users = _user_count(owner_session)
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(c, fake, idp, "c1", oid=str(uuid4()), email=email, xms_edov=True, **guest)
    assert r.headers["location"] == oidc_flow.FAIL and not _has_session(r)
    assert _identities(owner_session, idp) == []
    assert _link_pending(owner_session, victim)  # the link is still armed, unused
    assert _user_count(owner_session) == users  # no JIT user either
    assert _audit(owner_session, "identity.login.failed") == [("jit_requires_verified_email", None)]


VICTIM = "<victim>"  # replaced by the client-B person's address


@pytest.mark.parametrize(
    "claims",
    [
        {"acct": None, "email": None, "preferred_username": VICTIM},
        {"acct": None, "email": VICTIM, "xms_edov": True},
        {"acct": False, "email": VICTIM, "xms_edov": True, "preferred_username": VICTIM},
        {"acct": "", "email": VICTIM, "xms_edov": True, "preferred_username": VICTIM},
        {"acct": "2", "email": VICTIM, "xms_edov": True, "preferred_username": VICTIM},
        {"acct": True, "email": VICTIM, "xms_edov": True, "preferred_username": VICTIM},
    ],
    ids=[
        "no_acct_username",
        "no_acct_edov_email",
        "acct_false",
        "acct_empty",
        "acct_2",
        "acct_true",
    ],
)
def test_a_token_that_does_not_prove_a_member_never_links_another_clients_pending_user(
    settings: Settings, world: World, owner_session: Session, claims: dict[str, Any]
) -> None:
    """Isolation review 036-2: `acct` is optional, and a real guest's `preferred_username` is
    their home address, so only `acct` present and 0 proves a member (fail closed). Without that
    proof neither `email` (even with `xms_edov`) nor `preferred_username` is verified."""
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    victim, email = _pending_victim(owner_session, world)
    owner_session.commit()
    users = _user_count(owner_session)
    fake = _fake()
    token = {k: (email if v == VICTIM else v) for k, v in claims.items()}
    with entra_client(settings, fake) as c:
        r = sign_in(c, fake, idp, "c1", oid=str(uuid4()), **token)
    assert r.headers["location"] == oidc_flow.FAIL and not _has_session(r)
    assert _identities(owner_session, idp) == []
    assert _link_pending(owner_session, victim)
    assert _user_count(owner_session) == users
    assert _audit(owner_session, "identity.login.failed") == [("jit_requires_verified_email", None)]


@pytest.mark.parametrize("acct", [0, "0"], ids=["int", "str"])
def test_acct_zero_as_int_or_string_proves_a_member(
    settings: Settings, world: World, owner_session: Session, acct: object
) -> None:
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    victim, email = _pending_victim(owner_session, world)
    owner_session.commit()
    oid = str(uuid4())
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(c, fake, idp, "c1", oid=oid, acct=acct, email=email, xms_edov=True)
    assert r.headers["location"] == "/" and _has_session(r)
    assert _identities(owner_session, idp) == [(victim, oid)]


@pytest.mark.parametrize("edov", [None, False, "true"], ids=["absent", "false", "string"])
def test_an_email_without_xms_edov_is_not_verified(
    settings: Settings, world: World, owner_session: Session, edov: object
) -> None:
    """A member's `email` without `xms_edov` true (and no `preferred_username` to fall back
    on) is no verified e-mail: it links nobody and provisions nobody."""
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    victim, email = _pending_victim(owner_session, world)
    owner_session.commit()
    users = _user_count(owner_session)
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(
            c,
            fake,
            idp,
            "c1",
            oid=str(uuid4()),
            email=email,
            xms_edov=edov,
            preferred_username=None,
        )
    assert r.headers["location"] == oidc_flow.FAIL and not _has_session(r)
    assert _identities(owner_session, idp) == []
    assert _link_pending(owner_session, victim)
    assert _user_count(owner_session) == users


def test_a_member_with_xms_edov_links_the_pending_user(
    settings: Settings, world: World, owner_session: Session
) -> None:
    """Control for the two tests above: a member's verified `email` (`xms_edov` true)
    completes the armed link, as for any installation IdP."""
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    victim, email = _pending_victim(owner_session, world)
    owner_session.commit()
    oid = str(uuid4())
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(c, fake, idp, "c1", oid=oid, email=email, xms_edov=True)
    assert r.headers["location"] == "/" and _has_session(r)
    assert _identities(owner_session, idp) == [(victim, oid)]
    assert not _link_pending(owner_session, victim)


def test_a_member_without_xms_edov_is_provisioned_by_their_username(
    settings: Settings, world: World, owner_session: Session
) -> None:
    """`preferred_username` (the member's UPN) is the verified address when `email` is not
    vouched for: JIT uses it, never the unverified `email`."""
    idp = oidc_flow.owner_entra_idp(owner_session, TENANT)
    owner_session.commit()
    fake = _fake()
    with entra_client(settings, fake) as c:
        r = sign_in(
            c,
            fake,
            idp,
            "c1",
            oid=str(uuid4()),
            email="someone-else@b.test",
            preferred_username="Member@Contoso.test",
        )
    assert r.headers["location"] == "/" and _has_session(r)
    [(user, _)] = _identities(owner_session, idp)
    email = owner_session.execute(
        text("SELECT primary_email FROM shifde.users WHERE id = :u"), {"u": user}
    ).scalar_one()
    assert email == "member@contoso.test"
