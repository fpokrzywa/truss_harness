"""Secrets: envelope encryption, write-only API, audited resolution, redaction, KEK rotation."""

from __future__ import annotations

from uuid import UUID

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from shifde.authz.service import new_pdp
from shifde.core import db
from shifde.core.context import Principal, RequestContext
from shifde.core.crypto import KeyRing
from shifde.core.db import Database
from shifde.core.errors import NotFound
from shifde.core.settings import Settings
from shifde.secrets.service import SecretProvider
from tests._support.fullapp import World
from tests._support.p2util import login, post

A = "/api/v1/orgs/demo-a"


B = "/api/v1/orgs/demo-b"


ADMIN = "admin@demo-a.local"


VALUE = "Sup3r-S3cret-Value-0123456789"


runner = CliRunner()


def _ring(*kids: str) -> KeyRing:
    return KeyRing.parse(",".join(KeyRing.generate_entry(k) for k in kids))


def _provider_env(app_db: Database, settings: Settings, owner: Session, tenant: str, email: str):  # type: ignore[no-untyped-def]
    tid = owner.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": tenant}
    ).scalar_one()
    uid = owner.execute(
        text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
    ).scalar_one()
    owner.rollback()
    s = app_db.session()
    db.set_tenant(s, tid)
    db.set_principal(s, uid)
    principal = Principal(uid, email, email)
    ctx = RequestContext(request_id="resolve-test", principal=principal)
    return s, principal, new_pdp(s, principal, tid), SecretProvider(s, settings.keyring, ctx), tid


def test_resolve_is_pdp_checked_audited_and_logged(
    client: TestClient, world: World, app_db: Database, settings: Settings, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    sec = post(client, f"{A}/secrets", h, {"name": "svc-token", "value": VALUE})
    s, principal, pdp, provider, tid = _provider_env(
        app_db, settings, owner_session, "demo-a", ADMIN
    )
    try:
        value = provider.resolve(UUID(sec["id"]), principal, "connection.test", pdp=pdp)
        assert value.reveal() == VALUE and VALUE not in repr(value)
        s.commit()
    finally:
        s.close()
    log = owner_session.execute(
        text("SELECT purpose, outcome, principal_id FROM shifde.secret_access_log")
    ).one()
    assert (log[0], log[1]) == ("connection.test", "success") and log[2] is not None
    ev = owner_session.execute(
        text("SELECT details FROM shifde.audit_events WHERE action = 'secret.resolved'")
    ).scalar_one()
    assert ev["purpose"] == "connection.test" and VALUE not in str(ev)
    assert (
        owner_session.execute(text("SELECT last_used_at FROM shifde.secrets")).scalar_one()
        is not None
    )


def test_ciphertext_moved_to_another_secret_row_does_not_decrypt(
    client: TestClient, world: World, app_db: Database, settings: Settings, owner_session: Session
) -> None:
    h = login(client, world.passwords, ADMIN)
    one = post(client, f"{A}/secrets", h, {"name": "one", "value": "first-secret-value"})
    two = post(client, f"{A}/secrets", h, {"name": "two", "value": "second-secret-value"})
    owner_session.execute(
        text(
            "UPDATE shifde.secrets SET ciphertext = (SELECT ciphertext FROM shifde.secrets WHERE id = :a), nonce = (SELECT nonce FROM shifde.secrets WHERE id = :a), wrapped_dek = (SELECT wrapped_dek FROM shifde.secrets WHERE id = :a) WHERE id = :b"
        ),
        {"a": one["id"], "b": two["id"]},
    )
    owner_session.commit()
    s, principal, pdp, provider, _ = _provider_env(app_db, settings, owner_session, "demo-a", ADMIN)
    try:
        with pytest.raises(NotFound):  # AAD (secret id) mismatch -> authentication failure
            provider.resolve(UUID(two["id"]), principal, "x", pdp=pdp)
        assert (
            provider.resolve(UUID(one["id"]), principal, "x", pdp=pdp).reveal()
            == "first-secret-value"
        )
    finally:
        s.close()


def test_secrets_never_appear_in_responses_or_log_lines(
    client: TestClient,
    world: World,
    app_db: Database,
    settings: Settings,
    owner_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h = login(client, world.passwords, ADMIN)
    responses: list[str] = []
    sec = post(client, f"{A}/secrets", h, {"name": "leaky", "value": VALUE})
    responses.append(str(sec))
    sa = post(client, f"{A}/service-accounts", h, {"name": "bot"})
    key = post(
        client,
        f"{A}/api-keys",
        h,
        {
            "name": "k",
            "principal_type": "service_account",
            "principal_id": sa["id"],
            "scopes": ["workspace:read"],
            "expires_in_days": 5,
        },
    )
    api_secret = key["secret"]
    idp = post(
        client,
        f"{A}/idps",
        h,
        {
            "display_name": "i",
            "issuer": "https://i.example",
            "discovery_url": "https://i.example/.well-known/openid-configuration",
            "client_id": "c",
            "client_secret": "idp-client-secret-777",
        },
    )
    responses.append(str(idp))
    for url in (
        f"{A}/secrets",
        f"{A}/api-keys",
        f"{A}/idps",
        f"{A}/audit-events?limit=200",
        f"{A}/service-accounts",
    ):
        responses.append(client.get(url, headers=h).text)
    client.cookies.clear()
    r = client.get(f"{A}/workspaces", headers={"Authorization": f"Bearer {api_secret}"})
    responses.append(r.text)
    client.get(
        f"{A}/workspaces", headers={"Authorization": f"Bearer {api_secret[:-3]}xyz"}
    )  # wrong secret
    s, principal, pdp, provider, _ = _provider_env(app_db, settings, owner_session, "demo-a", ADMIN)
    try:
        provider.resolve(UUID(sec["id"]), principal, "leak.test", pdp=pdp)
        structlog.get_logger("leak.test").info(
            "oops", payload=VALUE, header=f"Bearer {api_secret}", url=f"postgresql://u:{VALUE}@h/db"
        )
        logging_msg = f"connection failed for password {VALUE}"
        import logging

        logging.getLogger("leak.test.stdlib").error(logging_msg)
        s.commit()
    finally:
        s.close()
    out = capsys.readouterr()
    haystack = out.out + out.err + "\n".join(responses)
    for needle in (VALUE, "idp-client-secret-777"):
        assert needle not in haystack, needle
    # the API key secret is in exactly one response: its creation
    assert api_secret in str(key)
    assert api_secret not in "\n".join(responses) and api_secret not in out.out + out.err
    assert "[REDACTED]" in out.out  # the deliberate log lines were scrubbed, not dropped


def uuid4() -> UUID:
    from uuid import uuid4 as _u

    return _u()
