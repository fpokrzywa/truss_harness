"""Administration of `entra` identity providers and of platform groups mapped by Entra object id
(SP3-6, issue #83; design SP3 section 4 and section 8; DECISIONS B-2711).

An `entra` IdP is installation-wide only (`/platform/idps`); its issuer, discovery URL and groups
claim are pinned to its Entra tenant, checked on create and on a patch merged with the stored row
(422), and the database refuses a row that breaks the profile rules
(`ck_idp_configs__entra_installation`, `ck_idp_configs__profile`). A platform group mapped to an
`entra` IdP names a GUID, stored lowercase. The app is `tests/admin/conftest.py::admin_client`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any  # Any: JSON bodies
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.fake_oidc import CLIENT_ID, ISSUER, entra_issuer, resolver
from tests.admin.conftest import admin_client
from tests.admin.test_idps import login, post
from tests.tenancy.conftest import World
from tests.tenancy.routes import People

TENANT = "3F2A1B4C-5D6E-4F70-8192-A3B4C5D6E7F8"  # as an administrator may paste it
LOWER = TENANT.lower()
ISSUER_ENTRA = entra_issuer(LOWER)
DISCOVERY_ENTRA = f"{ISSUER_ENTRA}/.well-known/openid-configuration"


@pytest.fixture
def env(settings: Settings, world: World) -> Iterator[TestClient]:
    with admin_client(settings, resolver=resolver) as c:
        yield c


def entra_body(**over: object) -> dict[str, object]:
    return {
        "display_name": "Contoso Entra",
        "issuer": ISSUER_ENTRA,
        "discovery_url": DISCOVERY_ENTRA,
        "client_id": CLIENT_ID,
        "jit": "create_user",
        "group_sync": "sync",
        "profile": "entra",
        "entra_tenant_id": TENANT,
        **over,
    }


def _create(c: TestClient, people: People, **over: object) -> dict[str, Any]:
    h = login(c, people.platform)
    out: dict[str, Any] = post(c, "/api/v1/platform/idps", h, entra_body(**over))
    return out


# ------------------------------------------------------------------ create
def test_the_platform_creates_an_entra_idp_and_reads_its_profile(
    env: TestClient, people: People, owner_session: Session
) -> None:
    created = _create(env, people)
    assert created["profile"] == "entra" and created["entra_tenant_id"] == LOWER
    assert created["tenant_id"] is None
    h = login(env, people.platform)
    got = env.get(f"/api/v1/platform/idps/{created['id']}", headers=h).json()
    assert (got["profile"], got["entra_tenant_id"]) == ("entra", LOWER)
    details = owner_session.execute(
        text(
            "SELECT details FROM shifde.audit_events WHERE action = 'identity.idp.created' "
            "AND target_id = :i"
        ),
        {"i": created["id"]},
    ).scalar_one()
    assert details["profile"] == "entra" and details["entra_tenant_id"] == LOWER


def test_a_generic_idp_reads_as_generic(env: TestClient, people: People) -> None:
    h = login(env, people.platform)
    out = post(
        env,
        "/api/v1/platform/idps",
        h,
        {
            "display_name": "Generic",
            "issuer": ISSUER,
            "discovery_url": f"{ISSUER}/.well-known/openid-configuration",
            "client_id": CLIENT_ID,
        },
    )
    assert (out["profile"], out["entra_tenant_id"]) == ("generic", None)


@pytest.mark.parametrize(
    "over",
    [
        {"entra_tenant_id": None},  # entra without a tenant
        {"profile": "generic"},  # a tenant id with the generic profile
        {"issuer": "https://login.microsoftonline.com/common/v2.0"},
        {"issuer": entra_issuer(str(uuid4()))},  # another tenant's issuer
        {"discovery_url": f"{ISSUER_ENTRA}/v2.0/.well-known/openid-configuration"},
        {"claim_mappings": {"email": "email", "groups": "roles"}},
    ],
    ids=["no_tenant", "generic_with_tenant", "common", "other_tenant", "discovery", "groups"],
)
def test_an_entra_idp_that_breaks_a_rule_is_refused(
    env: TestClient, people: People, owner_session: Session, over: dict[str, object]
) -> None:
    h = login(env, people.platform)
    post(env, "/api/v1/platform/idps", h, entra_body(**over), status=422)
    assert owner_session.execute(text("SELECT count(*) FROM shifde.idp_configs")).scalar_one() == 0


# ------------------------------------------------------------------ patch
@pytest.mark.parametrize(
    "patch",
    [
        {"issuer": "https://login.microsoftonline.com/common/v2.0"},
        {"discovery_url": "https://login.microsoftonline.com/common/v2.0/.well-known/x"},
        {"claim_mappings": {"groups": "roles"}},
        {"entra_tenant_id": str(uuid4())},  # the issuer no longer matches
        {"entra_tenant_id": None},
    ],
    ids=["issuer", "discovery", "groups", "tenant", "no_tenant"],
)
def test_a_patch_that_makes_an_entra_idp_invalid_is_refused(
    env: TestClient, people: People, patch: dict[str, object]
) -> None:
    created = _create(env, people)
    h = login(env, people.platform)
    url = f"/api/v1/platform/idps/{created['id']}"
    r = env.patch(url, json={**patch, "row_version": 1}, headers=h)
    assert r.status_code == 422, r.text
    assert env.get(url, headers=h).json()["row_version"] == 1


def test_a_patch_to_generic_clears_the_tenant_and_a_valid_patch_works(
    env: TestClient, people: People
) -> None:
    created = _create(env, people)
    h = login(env, people.platform)
    url = f"/api/v1/platform/idps/{created['id']}"
    renamed = env.patch(url, json={"display_name": "Contoso", "row_version": 1}, headers=h)
    assert renamed.status_code == 200 and renamed.json()["profile"] == "entra", renamed.text
    generic = env.patch(url, json={"profile": "generic", "row_version": 2}, headers=h)
    assert generic.status_code == 200, generic.text
    assert (generic.json()["profile"], generic.json()["entra_tenant_id"]) == ("generic", None)
    back = env.patch(url, json={"profile": "entra", "row_version": 3}, headers=h)
    assert back.status_code == 422  # entra again needs its tenant
    again = env.patch(
        url, json={"profile": "entra", "entra_tenant_id": LOWER, "row_version": 3}, headers=h
    )
    assert again.status_code == 200 and again.json()["entra_tenant_id"] == LOWER, again.text


# ------------------------------------------------------------------ client IdPs stay generic
def test_a_client_idp_cannot_use_the_entra_profile(
    env: TestClient, world: World, people: People, owner_session: Session
) -> None:
    h = login(env, people.owner_a)
    base = f"/api/v1/orgs/{world.a.slug}/idps"
    body = entra_body(jit="off", group_sync="off")
    refused = env.post(base, json=body, headers=h)
    assert refused.status_code == 422, refused.text
    generic = post(
        env,
        base,
        h,
        {
            "display_name": "Corp",
            "issuer": ISSUER,
            "discovery_url": f"{ISSUER}/.well-known/openid-configuration",
            "client_id": CLIENT_ID,
        },
    )
    patched = env.patch(
        f"{base}/{generic['id']}",
        json={
            "profile": "entra",
            "entra_tenant_id": LOWER,
            "issuer": ISSUER_ENTRA,
            "discovery_url": DISCOVERY_ENTRA,
            "row_version": 1,
        },
        headers=h,
    )
    assert patched.status_code == 422, patched.text
    profile = owner_session.execute(
        text("SELECT profile FROM shifde.idp_configs WHERE id = :i"), {"i": generic["id"]}
    ).scalar_one()
    assert profile == "generic"


@pytest.mark.parametrize(
    ("tenant", "profile", "entra_tenant", "constraint"),
    [
        (True, "entra", LOWER, "ck_idp_configs__entra_installation"),
        (False, "entra", None, "ck_idp_configs__entra_installation"),
        (False, "other", None, "ck_idp_configs__profile"),
    ],
)
def test_the_database_refuses_a_row_that_breaks_the_profile_rules(
    world: World,
    owner_session: Session,
    tenant: bool,
    profile: str,
    entra_tenant: str | None,
    constraint: str,
) -> None:
    with pytest.raises(DBAPIError, match=constraint):
        owner_session.execute(
            text(
                "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, "
                "discovery_url, client_id, profile, entra_tenant_id) "
                "VALUES (:id, :t, 'x', :i, :d, 'c', :p, :e)"
            ),
            {
                "id": uuid7(),
                "t": world.a.tenant if tenant else None,
                "i": ISSUER_ENTRA,
                "d": DISCOVERY_ENTRA,
                "p": profile,
                "e": entra_tenant,
            },
        )
    owner_session.rollback()


# ------------------------------------------------------------------ groups by object id
def test_a_group_of_an_entra_idp_is_mapped_by_a_guid_stored_lowercase(
    env: TestClient, people: People, owner_session: Session
) -> None:
    created = _create(env, people)
    h = login(env, people.platform)

    def group(external_id: str) -> Any:
        body = {
            "slug": f"entra-{uuid7().hex[-8:]}",
            "name": "Entra readers",
            "idp_config_id": created["id"],
            "external_id": external_id,
        }
        return env.post("/api/v1/platform/groups", json=body, headers=h)

    refused = group("not-a-guid")
    assert refused.status_code == 422, refused.text
    oid = str(uuid4())
    made = group(oid.upper())
    assert made.status_code == 201, made.text
    assert made.json()["external_id"] == oid
    stored = owner_session.execute(
        text("SELECT external_id FROM shifde.groups WHERE id = :g"), {"g": made.json()["id"]}
    ).scalar_one()
    assert stored == oid
    assert UUID(stored)  # a GUID


def test_a_group_of_a_generic_idp_keeps_its_external_id(env: TestClient, people: People) -> None:
    h = login(env, people.platform)
    idp = post(
        env,
        "/api/v1/platform/idps",
        h,
        {
            "display_name": "Generic",
            "issuer": ISSUER,
            "discovery_url": f"{ISSUER}/.well-known/openid-configuration",
            "client_id": CLIENT_ID,
        },
    )
    made = env.post(
        "/api/v1/platform/groups",
        json={
            "slug": f"eng-{uuid7().hex[-8:]}",
            "name": "Eng",
            "idp_config_id": idp["id"],
            "external_id": "Data Eng",
        },
        headers=h,
    )
    assert made.status_code == 201 and made.json()["external_id"] == "Data Eng", made.text
