"""The secrets service, as `shifde_app`: envelope encryption, value and key rotation, PDP-gated
use, the access log and audit row of a resolution, redaction, and what reads return.

SETH 3ba0e06 `tests/test_p2_secrets.py::test_resolve_is_pdp_checked_audited_and_logged`,
`..._denied_for_principals_without_secret_use_...` and `test_ciphertext_moved_to_another_secret_
row_does_not_decrypt` need the app `World` and move with admin unchanged; the tests below check
the same behaviour through the service. Audit rows for create, rotate and disable are written by
SETH's admin routes (`admin/providers.py`), not by this module: they are tested with admin."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.context import RequestContext
from shifde.core.crypto import KeyRing
from shifde.core.db import Database, platform_admin_context
from shifde.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.redaction import scrub_text
from shifde.secrets import service as secrets_service
from shifde.secrets.service import MAX_SECRET_BYTES, SecretMeta, SecretProvider
from tests.secrets.conftest import Clients, app_session, caller

VALUE = "Sup3r-S3cret-Value-0123456789"
MATERIAL = ("ciphertext", "wrapped_dek", "nonce", "kek_id", "external_ref")


@pytest.fixture
def a_session(app_db: Database, clients: Clients) -> Iterator[Session]:
    s = app_session(app_db, clients.a.tenant, clients.a.admin)
    yield s
    s.close()


def _create(s: Session, keyring: KeyRing, tenant: UUID, name: str, value: str) -> SecretMeta:
    sec = secrets_service.create_secret(
        s, keyring, tenant_id=tenant, name=name, value=value, created_by=None
    )
    s.commit()
    return sec


def _row(owner: Session, secret_id: UUID) -> dict[str, object]:
    row = owner.execute(
        text("SELECT * FROM shifde.secrets WHERE id = :i"), {"i": secret_id}
    ).mappings()
    found = dict(row.one())
    owner.rollback()
    return found


# ---------------------------------------------------------------- envelope encryption
def test_create_stores_only_ciphertext_and_reads_return_no_key_material(
    a_session: Session, owner_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    sec = _create(a_session, keyring, clients.a.tenant, "db-password", VALUE)
    stored = _row(owner_session, sec.id)
    assert stored["provider_key"] == "db_encrypted" and stored["kek_id"] == "k1"
    for column in ("ciphertext", "wrapped_dek", "nonce"):
        raw = stored[column]
        assert isinstance(raw, bytes) and raw and VALUE.encode() not in raw
    fields = {f.name for f in dataclasses.fields(SecretMeta)}
    assert not fields & set(MATERIAL)
    got = secrets_service.get_secret(a_session, sec.id, tenant_id=clients.a.tenant)
    listed = secrets_service.list_secrets(
        a_session, tenant_id=clients.a.tenant, after=None, limit=10
    )
    assert got == sec and listed == [sec]
    for meta in (sec, got, *listed):
        assert isinstance(meta, SecretMeta)
        assert not any(hasattr(meta, name) for name in MATERIAL)
        assert VALUE not in repr(meta)


def test_resolve_round_trip_is_logged_and_audited_without_the_value(
    a_session: Session, owner_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    sec = _create(a_session, keyring, clients.a.tenant, "svc-token", VALUE)
    c = caller(a_session, keyring, clients.a.tenant, clients.a.admin)
    value = c.provider.resolve(sec.id, c.principal, "connection.test", pdp=c.pdp)
    assert value.reveal() == VALUE and VALUE not in repr(value) and VALUE not in str(value)
    a_session.commit()
    log = owner_session.execute(
        text(
            "SELECT tenant_id, secret_id, purpose, outcome, principal_id FROM shifde.secret_access_log"
        )
    ).one()
    assert tuple(log) == (clients.a.tenant, sec.id, "connection.test", "success", clients.a.admin)
    ev = owner_session.execute(
        text(
            "SELECT tenant_id, target_id, details FROM shifde.audit_events "
            "WHERE action = 'secret.resolved'"
        )
    ).one()
    assert ev[0] == clients.a.tenant and ev[1] == str(sec.id)
    assert (ev[2]["purpose"], ev[2]["version"]) == ("connection.test", 1)
    assert VALUE not in str(ev)
    assert _row(owner_session, sec.id)["last_used_at"] is not None


def test_value_rotation_bumps_versions_and_replaces_the_envelope(
    a_session: Session, owner_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    sec = _create(a_session, keyring, clients.a.tenant, "rotating", "first-value-1234")
    before = _row(owner_session, sec.id)
    rotated = secrets_service.rotate_secret(
        a_session, keyring, sec, value="second-value-5678", expected_version=sec.row_version
    )
    a_session.commit()
    assert (rotated.secret_version, rotated.row_version) == (2, 2)
    assert rotated.rotated_at is not None
    after = _row(owner_session, sec.id)
    assert after["ciphertext"] != before["ciphertext"] and after["nonce"] != before["nonce"]
    c = caller(a_session, keyring, clients.a.tenant, clients.a.admin)
    assert c.provider.resolve(sec.id, c.principal, "x", pdp=c.pdp).reveal() == "second-value-5678"
    a_session.commit()
    with pytest.raises(Conflict, match="row_version mismatch"):
        secrets_service.rotate_secret(
            a_session, keyring, rotated, value="third-value-0000", expected_version=1
        )
    a_session.rollback()
    disabled = secrets_service.disable_secret(a_session, rotated, expected_version=2)
    a_session.commit()
    assert disabled.status == "disabled"
    with pytest.raises(Conflict, match="disabled"):
        secrets_service.rotate_secret(
            a_session, keyring, disabled, value="third-value-0000", expected_version=None
        )
    with pytest.raises(NotFound):
        c.provider.resolve(sec.id, c.principal, "x", pdp=c.pdp)


def test_key_rotation_rewraps_every_dek_under_the_active_key(
    owner_db: Database, app_db: Database, keyring: KeyRing, clients: Clients
) -> None:
    secrets = {}
    for cl in (clients.a, clients.b):
        with app_session(app_db, cl.tenant) as s:
            secrets[cl.tenant] = _create(s, keyring, cl.tenant, "kek-rotated", f"value-{cl.tenant}")
    both = KeyRing({"k2": os.urandom(32), "k1": keyring.kek("k1")}, active="k2")
    with owner_db.session() as owner:  # the CLI path: the owner sees every client's rows
        assert secrets_service.rewrap_all(owner, both) == (2, 2)
        owner.commit()
        assert secrets_service.rewrap_all(owner, both) == (0, 2)  # idempotent
        kids = owner.execute(text("SELECT DISTINCT kek_id FROM shifde.secrets")).scalars().all()
        assert kids == ["k2"]
    only_new = KeyRing({"k2": both.kek("k2")}, active="k2")
    for cl in (clients.a, clients.b):
        with app_session(app_db, cl.tenant) as s:
            for ring, expected in ((only_new, f"value-{cl.tenant}"), (keyring, None)):
                c = caller(s, ring, cl.tenant, cl.admin)
                if expected is None:  # the retired key alone can no longer open it
                    with pytest.raises(NotFound, match="could not be decrypted"):
                        c.provider.resolve(secrets[cl.tenant].id, c.principal, "x", pdp=c.pdp)
                    s.rollback()
                else:
                    got = c.provider.resolve(secrets[cl.tenant].id, c.principal, "x", pdp=c.pdp)
                    assert got.reveal() == expected
                    s.commit()


def test_a_wrong_key_is_refused(a_session: Session, keyring: KeyRing, clients: Clients) -> None:
    sec = _create(a_session, keyring, clients.a.tenant, "keyed", VALUE)
    same_kid_other_key = KeyRing({"k1": os.urandom(32)}, active="k1")
    other_kid = KeyRing.parse(KeyRing.generate_entry("k9"))
    for ring in (same_kid_other_key, other_kid):
        c = caller(a_session, ring, clients.a.tenant, clients.a.admin)
        with pytest.raises(NotFound, match="could not be decrypted"):
            c.provider.resolve(sec.id, c.principal, "x", pdp=c.pdp)
        a_session.rollback()


def test_ciphertext_moved_to_another_secret_row_does_not_decrypt(
    a_session: Session, owner_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    one = _create(a_session, keyring, clients.a.tenant, "one", "first-secret-value")
    two = _create(a_session, keyring, clients.a.tenant, "two", "second-secret-value")
    owner_session.execute(
        text(
            "UPDATE shifde.secrets SET ciphertext = o.ciphertext, nonce = o.nonce, "
            "wrapped_dek = o.wrapped_dek FROM shifde.secrets o WHERE o.id = :a "
            "AND shifde.secrets.id = :b"
        ),
        {"a": one.id, "b": two.id},
    )
    owner_session.commit()
    c = caller(a_session, keyring, clients.a.tenant, clients.a.admin)
    with pytest.raises(NotFound):  # AAD (secret id) mismatch -> authentication failure
        c.provider.resolve(two.id, c.principal, "x", pdp=c.pdp)
    a_session.rollback()
    assert c.provider.resolve(one.id, c.principal, "x", pdp=c.pdp).reveal() == "first-secret-value"


# ---------------------------------------------------------------- PDP-gated use
def test_use_needs_secret_use_and_hides_the_secret_without_secret_read(
    app_db: Database, owner_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    with app_session(app_db, clients.a.tenant) as s:
        sec = _create(s, keyring, clients.a.tenant, "gated", VALUE)
        nobody = caller(s, keyring, clients.a.tenant, clients.a.nobody)
        with pytest.raises(NotFound, match="not found"):  # no secret:read: it does not exist
            nobody.provider.resolve(sec.id, nobody.principal, "x", pdp=nobody.pdp)
        s.rollback()
        reader = caller(s, keyring, clients.a.tenant, clients.a.auditor)
        with pytest.raises(Forbidden, match="secret:use"):  # secret:read but no secret:use
            reader.provider.resolve(sec.id, reader.principal, "x", pdp=reader.pdp)
        s.rollback()
    count = owner_session.execute(text("SELECT count(*) FROM shifde.secret_access_log"))
    assert count.scalar_one() == 0
    resolved = owner_session.execute(
        text("SELECT count(*) FROM shifde.audit_events WHERE action = 'secret.resolved'")
    )
    assert resolved.scalar_one() == 0


def test_use_without_a_tenant_context_finds_nothing(
    app_db: Database, keyring: KeyRing, clients: Clients
) -> None:
    with app_session(app_db, clients.a.tenant) as s:
        sec = _create(s, keyring, clients.a.tenant, "ctx", VALUE)
    with app_session(app_db, None) as s:
        c = caller(s, keyring, clients.a.tenant, clients.a.admin)
        with pytest.raises(NotFound, match="secret not found"):
            c.provider.resolve(sec.id, c.principal, "x", pdp=c.pdp)


# ---------------------------------------------------------------- system resolution
def test_resolve_system_is_keyed_to_the_named_tenant(
    a_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    sec = _create(a_session, keyring, clients.a.tenant, "conn-cred", VALUE)
    provider = SecretProvider(a_session, keyring, RequestContext(request_id="system"))
    got = provider.resolve_system(sec.id, "connection:x:test", tenant_id=clients.a.tenant)
    assert got.reveal() == VALUE
    a_session.commit()
    for tenant in (None, clients.b.tenant):
        with pytest.raises(NotFound, match="secret not found"):
            provider.resolve_system(sec.id, "connection:x:test", tenant_id=tenant)


def test_installation_secrets_resolve_only_in_the_platform_admin_context(
    app_db: Database, owner_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    with app_session(app_db, None) as s:
        with platform_admin_context(s):
            sec = secrets_service.create_secret(
                s, keyring, tenant_id=None, name="idp", value=VALUE, created_by=None
            )
            provider = SecretProvider(s, keyring, RequestContext(request_id="oidc"))
            got = provider.resolve_system(sec.id, "oidc.token_exchange", tenant_id=None)
            assert got.reveal() == VALUE
        s.commit()
    ev = owner_session.execute(
        text(
            "SELECT tenant_id, actor_type, details FROM shifde.audit_events "
            "WHERE action = 'secret.resolved'"
        )
    ).one()
    assert ev[0] is None and ev[2]["actor"] == "system"
    assert ev[2]["purpose"] == "oidc.token_exchange"
    owner_session.rollback()
    with app_session(app_db, clients.a.tenant, clients.a.admin) as s:
        provider = SecretProvider(s, keyring, RequestContext(request_id="client"))
        for tenant in (None, clients.a.tenant):
            with pytest.raises(NotFound, match="secret not found"):
                provider.resolve_system(sec.id, "x", tenant_id=tenant)
        c = caller(s, keyring, clients.a.tenant, clients.a.admin)
        with pytest.raises(NotFound, match="secret not found"):
            c.provider.resolve(sec.id, c.principal, "x", pdp=c.pdp)
        assert secrets_service.get_secret(s, sec.id, tenant_id=None) is None
        assert secrets_service.list_secrets(s, tenant_id=None, after=None, limit=10) == []


def test_the_env_provider_never_serves_a_client_secret(
    a_session: Session,
    owner_session: Session,
    keyring: KeyRing,
    clients: Clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client row of provider `env` would read the server's environment (the key ring lives
    there): refused whoever wrote the row (B-804)."""
    monkeypatch.setenv("SHIFDE_TEST_SERVER_ONLY", "server-environment-value")
    sid = uuid7()
    owner_session.execute(
        text(
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
            "VALUES (:i, :t, 'env-ref', 'env', 'SHIFDE_TEST_SERVER_ONLY')"
        ),
        {"i": sid, "t": clients.a.tenant},
    )
    owner_session.commit()
    c = caller(a_session, keyring, clients.a.tenant, clients.a.admin)
    with pytest.raises(NotFound, match="secret not found"):
        c.provider.resolve(sid, c.principal, "x", pdp=c.pdp)
    a_session.rollback()
    with pytest.raises(NotFound, match="secret not found"):
        c.provider.resolve_system(sid, "x", tenant_id=clients.a.tenant)


# ---------------------------------------------------------------- validation and redaction
def test_names_are_unique_per_client_and_values_are_validated(
    app_db: Database, keyring: KeyRing, clients: Clients
) -> None:
    with app_session(app_db, clients.a.tenant) as s:
        _create(s, keyring, clients.a.tenant, "shared-name", VALUE)
        with pytest.raises(Conflict, match="already exists"):
            _create(s, keyring, clients.a.tenant, "shared-name", "another-value-123")
        for bad in ("", "x" * (MAX_SECRET_BYTES + 1)):
            with pytest.raises(ValidationFailed):
                _create(s, keyring, clients.a.tenant, "bad", bad)
        with pytest.raises(ValidationFailed, match="kind"):
            secrets_service.create_secret(
                s,
                keyring,
                tenant_id=clients.a.tenant,
                name="k",
                value=VALUE,
                kind="nope",
                created_by=None,
            )
    with app_session(app_db, clients.b.tenant) as s:  # another client may use the same name
        assert _create(s, keyring, clients.b.tenant, "shared-name", VALUE).name == "shared-name"


def test_written_and_resolved_values_are_registered_for_redaction(
    a_session: Session, keyring: KeyRing, clients: Clients
) -> None:
    created, rotated = "created-value-a1b2c3d4", "rotated-value-e5f6g7h8"
    sec = _create(a_session, keyring, clients.a.tenant, "redacted", created)
    assert created not in scrub_text(f"log line {created}")
    sec = secrets_service.rotate_secret(
        a_session, keyring, sec, value=rotated, expected_version=None
    )
    a_session.commit()
    assert rotated not in scrub_text(f"log line {rotated}")
    c = caller(a_session, keyring, clients.a.tenant, clients.a.admin)
    value = c.provider.resolve(sec.id, c.principal, "x", pdp=c.pdp)
    assert value.reveal() == rotated and rotated not in scrub_text(f"{value.reveal()}")
