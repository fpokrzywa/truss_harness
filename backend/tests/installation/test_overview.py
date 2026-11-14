"""The platform installation overview (SETH 3ba0e06 `test_p9_installation.py`, minimal port).

`test_installation_overview_is_platform_admin_only` keeps SETH's assertions (401 unauthenticated,
403 for a client's administrator, 200 for the platform administrator) over the minimal app. The
other overview tests of SETH apply a profile, read the dropped `profile`, `retention`,
`governance_defaults` and `section_status` fields, or need the `workers` and `egress_policies`
tables of later modules; they are replaced by the tests below or deferred (docs/build/
carry_forward.md, B-1101..B-1106).
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import __version__
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.tenants import add_tenants
from tests.identity.harness import login, person

URL = "/api/v1/platform/installation"
PLATFORM = "platform@shifde.local"
TENANT_ADMIN = "admin@demo-a.local"
STAFF = "staff@shi.test"
CLIENT_NAME = "Zebra Holdings Confidential"
CLIENT_IDP = "Client Secret Corp IdP"


def _member_of_a_client(owner: Session, email: str) -> UUID:
    uid = person(owner, email)
    tenant = uuid7()
    add_tenants(owner, tenant)
    owner.execute(
        text("UPDATE shifde.tenants SET name = :n WHERE id = :t"), {"n": CLIENT_NAME, "t": tenant}
    )
    owner.execute(
        text("INSERT INTO shifde.memberships (id, tenant_id, user_id) VALUES (:id, :t, :u)"),
        {"id": uuid7(), "t": tenant, "u": uid},
    )
    owner.commit()
    return tenant


def _idp(owner: Session, tenant: UUID | None, name: str, issuer: str) -> None:
    owner.execute(
        text(
            "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, discovery_url, "
            "client_id) VALUES (:id, :t, :n, :i, :d, 'client-1')"
        ),
        {"id": uuid7(), "t": tenant, "n": name, "i": issuer, "d": f"{issuer}/.well-known"},
    )
    owner.commit()


def test_installation_overview_is_platform_admin_only(
    client: TestClient, owner_session: Session
) -> None:
    _member_of_a_client(owner_session, TENANT_ADMIN)
    person(owner_session, PLATFORM, admin=True)
    assert client.get(URL).status_code == 401
    login(client, TENANT_ADMIN)
    denied = client.get(URL)
    assert denied.status_code == 403
    client.cookies.clear()
    login(client, PLATFORM)
    ok = client.get(URL)
    assert ok.status_code == 200, ok.text


def test_staff_without_the_admin_flag_and_a_client_context_user_are_refused(
    client: TestClient, owner_session: Session
) -> None:
    person(owner_session, STAFF)  # a person with no membership and no admin flag
    login(client, STAFF)
    assert client.get(URL).status_code == 403
    client.cookies.clear()
    _member_of_a_client(owner_session, TENANT_ADMIN)
    login(client, TENANT_ADMIN)
    refused = client.get(URL)
    assert refused.status_code == 403
    assert CLIENT_NAME not in refused.text


def test_overview_shape_defaults_and_settings(
    client: TestClient, owner_session: Session, settings: Settings
) -> None:
    person(owner_session, PLATFORM, admin=True)
    login(client, PLATFORM)
    j = client.get(URL).json()
    assert list(j) == [
        "version", "env", "deployment_mode", "migration", "feature_flags", "auth", "security",
        "limits", "workers",
    ]  # fmt: skip
    assert j["version"] == __version__ and j["env"] == "test"
    assert j["deployment_mode"] == settings.deployment_mode
    assert j["migration"]["ok"] is True
    assert j["migration"]["current"] == j["migration"]["expected_head"]
    assert j["auth"] == {"local_enabled": True, "oidc_providers": []}
    assert j["security"] == {
        "session_idle_minutes": settings.session_idle_minutes,
        "session_absolute_hours": settings.session_absolute_hours,
        "api_key_max_days": settings.api_key_max_days,
        "egress_default_allow_private": False,
    }
    assert j["limits"] == {
        "max_run_seconds": settings.run_default_deadline_seconds,
        "max_tool_calls": 500,
        "inline_output_max_bytes": 65536,
    }
    assert j["feature_flags"] == {}
    assert j["workers"] is None or isinstance(j["workers"], list)


def test_installation_overview_reports_allow_private_egress_default(
    client: TestClient, owner_session: Session
) -> None:
    """SETH `test_p9_installation.py`, moved with the connections port (egress_policies exists
    and `shifde_app` holds SELECT on it, carry_forward B-1105); SETH's world login is replaced by
    this module's platform administrator."""
    person(owner_session, PLATFORM, admin=True)
    owner_session.execute(
        text(
            "UPDATE shifde.egress_policies SET allow_private = true "
            "WHERE tenant_id IS NULL AND is_default"
        )
    )
    owner_session.commit()
    try:
        login(client, PLATFORM)
        j = client.get(URL).json()
        assert j["security"]["egress_default_allow_private"] is True
    finally:
        owner_session.execute(
            text(
                "UPDATE shifde.egress_policies SET allow_private = false "
                "WHERE tenant_id IS NULL AND is_default"
            )
        )
        owner_session.commit()


def test_feature_flags_and_default_limits_come_from_installation_settings(
    client: TestClient, owner_session: Session
) -> None:
    owner_session.execute(
        text(
            "UPDATE shifde.installation_settings SET feature_flags = CAST(:f AS jsonb), "
            "default_limits = CAST(:l AS jsonb) WHERE id = 1"
        ),
        {
            "f": json.dumps({"pipelines": False, "beta": True}),
            "l": json.dumps({"max_run_seconds": 900, "max_tool_calls": 50}),
        },
    )
    owner_session.commit()
    person(owner_session, PLATFORM, admin=True)
    login(client, PLATFORM)
    j = client.get(URL).json()
    assert j["feature_flags"] == {"pipelines": False, "beta": True}
    assert j["limits"] == {
        "max_run_seconds": 900,
        "max_tool_calls": 50,
        "inline_output_max_bytes": 65536,
    }


def test_overview_lists_installation_idps_only_and_carries_no_client_content_or_secrets(
    client: TestClient, owner_session: Session, settings: Settings
) -> None:
    tenant = _member_of_a_client(owner_session, TENANT_ADMIN)
    _idp(owner_session, None, "Corporate IdP", "https://login.example.test/realm")
    _idp(owner_session, tenant, CLIENT_IDP, "https://sso.zebra-client.test/realm")
    person(owner_session, PLATFORM, admin=True)
    login(client, PLATFORM)
    r = client.get(URL)
    assert r.status_code == 200
    assert r.json()["auth"]["oidc_providers"] == [
        {"name": "Corporate IdP", "issuer_host": "login.example.test"}
    ]
    body = r.text.lower()
    for forbidden in (
        CLIENT_NAME,
        CLIENT_IDP,
        "zebra-client.test",
        str(tenant),
        TENANT_ADMIN,
        "password",
        "postgresql+psycopg",
        "client_secret",
        settings.database_url,
    ):
        assert forbidden.lower() not in body


@pytest.mark.parametrize("path", ["/api/installation/branding", "/api/installation/branding/logo"])
def test_branding_routes_are_not_ported(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404
