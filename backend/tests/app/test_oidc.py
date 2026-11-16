"""SETH 3ba0e06 `test_p2_oidc.py` tests that need the mounted OpenID Connect routes (B-2263, B-2264),
over the full app and the fake IdP (`tests/_support/fake_oidc.py`).

Groups are platform-level here (identity_0005, B-928..B-932): SETH's client IdP with group sync
and on-demand group creation are replaced by an installation IdP (created through
`/platform/idps`, with a client secret read by `shifde_installation_idp_secret`, B-2260) and
platform groups mapped to it (`/platform/groups`), bound in demo-a by its owner. The route
assertions are SETH's; the setup changes are recorded in B-2264.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fake_oidc import FakeOidcProvider
from tests._support.fullapp import World, login
from tests.app.oidc_helpers import (
    PLATFORM,
    bind_group,
    create_idp,
    events,
    link,
    new_person,
    oidc_app,
    ok_login,
    platform_group,
    tenant_id,
    user_id,
)


@pytest.fixture
def provider() -> FakeOidcProvider:
    return FakeOidcProvider()


@pytest.fixture
def env(settings: Settings, world: World, provider: FakeOidcProvider) -> Iterator[TestClient]:
    with oidc_app(settings, provider) as c:
        yield c


def test_idp_groups_are_synced_on_login_and_removed_when_the_claim_shrinks(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world, group_sync="sync")
    platform_group(env, world, "platform-team", idp, "Platform Team")
    platform_group(env, world, "data-eng", idp, "Data Eng")
    person = new_person(owner_session, "newuser@example.com")
    link(owner_session, person, idp, "sub-123")
    r = ok_login(env, provider, idp, groups=["Platform Team", "Data Eng"])
    assert r.status_code == 302 and r.headers["location"] == "/", r.headers
    rows = owner_session.execute(
        text(
            "SELECT g.name, g.source, gm.source FROM shifde.groups g JOIN shifde.group_members gm "
            "ON gm.group_id = g.id WHERE gm.user_id = :u ORDER BY g.name"
        ),
        {"u": person},
    ).all()
    assert [tuple(r) for r in rows] == [
        ("Data Eng", "idp", "idp_sync"),
        ("Platform Team", "idp", "idp_sync"),
    ]
    env.cookies.clear()
    ok_login(env, provider, idp, code="c2", groups=["Data Eng"])
    owner_session.rollback()
    left = (
        owner_session.execute(
            text(
                "SELECT g.name FROM shifde.groups g JOIN shifde.group_members gm "
                "ON gm.group_id = g.id WHERE gm.user_id = :u"
            ),
            {"u": person},
        )
        .scalars()
        .all()
    )
    assert left == ["Data Eng"]
    # IdP-synced groups are read-only through the API
    env.cookies.clear()
    h = login(env, world, PLATFORM)
    groups = env.get("/api/v1/platform/groups", headers=h).json()["items"]
    g = next(x for x in groups if x["name"] == "Data Eng")
    assert g["source"] == "idp"
    r = env.put(
        f"/api/v1/platform/groups/{g['id']}/members",
        json={"user_ids": [], "row_version": g["row_version"]},
        headers=h,
    )
    assert r.status_code == 409


def test_idp_group_grants_flow_through_to_permissions(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    idp = create_idp(env, world, group_sync="sync")
    readers = platform_group(env, world, "readers", idp, "Readers")
    a = tenant_id(owner_session, "demo-a")
    person = new_person(owner_session, "newuser@example.com")
    link(owner_session, person, idp, "sub-123")
    base = "/api/v1/orgs/demo-a"
    bind_group(env, world, "demo-a", readers, ["workspace:read"], a)
    ok_login(env, provider, idp, groups=["Readers"])
    assert env.get(f"{base}/me/permissions").json()["permissions"] == ["workspace:read"]
    env.cookies.clear()
    ok_login(env, provider, idp, code="c2", groups=[])  # removed at the IdP -> revoked at login
    r = env.get(f"{base}/me/permissions")
    # SETH: 200 with no permissions; here, with no binding left, the organization is invisible
    # to the person (B-1009), so the same revocation is a 404 (B-2264)
    assert r.status_code == 404, r.text


def test_a_platform_administrator_synced_into_a_group_bound_in_a_is_a_self_add_in_a_only(
    env: TestClient, world: World, provider: FakeOidcProvider, owner_session: Session
) -> None:
    """B-1624 over the mounted routes (B-2232 follow-up 2): the IdP-sync add of a platform
    administrator into a group bound in A is `tenancy.platform_admin.self_added` and a counts-only
    `identity.group.members_changed` in A, shown to A's owner through A's audit route; nothing
    is filed in B; the installation IdP's secret was read without the administrator flag
    (`secret.idp_client_secret.resolved` at installation level, B-2260)."""
    idp = create_idp(env, world, group_sync="sync")
    ops = platform_group(env, world, "ops", idp, "Ops")
    a, b = tenant_id(owner_session, "demo-a"), tenant_id(owner_session, "demo-b")
    bind_group(env, world, "demo-a", ops, ["workspace:read"], a)
    admin = user_id(owner_session, PLATFORM)
    link(owner_session, admin, idp, "padmin")
    r = ok_login(env, provider, idp, sub="padmin", email=PLATFORM, groups=["Ops"])
    assert r.status_code == 302 and r.headers["location"] == "/", r.headers
    env.cookies.clear()
    adds = events(owner_session, "tenancy.platform_admin.self_added", a)
    assert [(d["through_group_id"], d["through_idp_sync"]) for d in adds] == [(ops, True)]
    changed = events(owner_session, "identity.group.members_changed", a)
    assert [(d["group_id"], d["added_count"], d["source"]) for d in changed] == [
        (ops, 1, "idp_sync")
    ]
    assert str(admin) not in str(changed)
    for action in ("tenancy.platform_admin.self_added", "identity.group.members_changed"):
        assert events(owner_session, action, b) == []
    h = login(env, world, "admin@demo-a.local")
    shown = env.get(
        "/api/v1/orgs/demo-a/audit-events",
        params={"action": "tenancy.platform_admin.self_added"},
        headers=h,
    )
    assert shown.status_code == 200 and len(shown.json()["items"]) == 1, shown.text
    env.cookies.clear()
    h = login(env, world, "admin@demo-b.local")
    shown = env.get(
        "/api/v1/orgs/demo-b/audit-events",
        params={"action": "tenancy.platform_admin.self_added"},
        headers=h,
    )
    assert shown.status_code == 200 and shown.json()["items"] == []
    resolved = events(owner_session, "secret.idp_client_secret.resolved", None)
    assert [(d["purpose"], d["idp_id"]) for d in resolved] == [("oidc.token_exchange", idp)]
