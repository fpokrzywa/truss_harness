"""Cross-client isolation of secrets, as `shifde_app` (each session asserts `current_user`).

Client A cannot read, list, use, rotate, disable or overwrite client B's secret by id, through
the service or raw SQL; B's secrets are absent from A's lists; with no tenant context nothing is
visible; the platform-administrator flag reaches installation-level rows only, never a client's;
and a refusal for B's id is the same as for an id that does not exist.

The module has no routes yet: the secret API arrives with admin (and the resolve paths with
connections and identity), whose cross-client tests cover the routes (docs/build/carry_forward.md).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.context import RequestContext
from shifde.core.crypto import KeyRing
from shifde.core.db import Database, platform_admin_context, set_tenant
from shifde.core.errors import AppError
from shifde.core.ids import uuid7
from shifde.secrets import service as secrets_service
from shifde.secrets.service import SecretMeta, SecretProvider
from tests.secrets.conftest import Clients, app_session, caller

B_VALUE = "b-client-secret-value-0001"


@dataclass(frozen=True)
class World:
    clients: Clients
    a_secret: SecretMeta
    b_secret: SecretMeta
    installation: SecretMeta


@pytest.fixture
def world(app_db: Database, keyring: KeyRing, clients: Clients) -> World:
    made: dict[UUID | None, SecretMeta] = {}
    for tenant, value in (
        (clients.a.tenant, "a-client-secret-value-0001"),
        (clients.b.tenant, B_VALUE),
        (None, "installation-secret-value-01"),
    ):
        with app_session(app_db, tenant) as s:
            if tenant is None:
                with platform_admin_context(s):
                    made[tenant] = secrets_service.create_secret(
                        s, keyring, tenant_id=None, name="same-name", value=value, created_by=None
                    )
            else:
                made[tenant] = secrets_service.create_secret(
                    s, keyring, tenant_id=tenant, name="same-name", value=value, created_by=None
                )
            s.commit()
    # B has used its secret once, so it has an access-log row
    with app_session(app_db, clients.b.tenant) as s:
        b = caller(s, keyring, clients.b.tenant, clients.b.admin)
        b.provider.resolve(made[clients.b.tenant].id, b.principal, "b.use", pdp=b.pdp)
        s.commit()
    return World(clients, made[clients.a.tenant], made[clients.b.tenant], made[None])


@pytest.fixture
def a_session(app_db: Database, world: World) -> Iterator[Session]:
    s = app_session(app_db, world.clients.a.tenant, world.clients.a.admin)
    yield s
    s.close()


def _ids(s: Session, table: str) -> set[UUID]:
    return {r[0] for r in s.execute(text(f"SELECT id FROM shifde.{table}"))}


def _refusal(fn: Callable[[], object]) -> tuple[type[Exception], str]:
    with pytest.raises(AppError) as err:
        fn()
    return type(err.value), str(err.value)


def _sqlstate(err: pytest.ExceptionInfo[DBAPIError]) -> str | None:
    return getattr(err.value.orig, "sqlstate", None)


def _b_row(owner_db: Database, world: World) -> tuple[object, ...]:
    with owner_db.session() as o:
        row = o.execute(
            text(
                "SELECT ciphertext, wrapped_dek, nonce, kek_id, secret_version, status, "
                "row_version, last_used_at FROM shifde.secrets WHERE id = :i"
            ),
            {"i": world.b_secret.id},
        ).one()
        return tuple(row)


# ---------------------------------------------------------------- read and list
def test_a_reads_and_lists_only_its_own_secrets(a_session: Session, world: World) -> None:
    a, b = world.clients.a.tenant, world.clients.b.tenant
    assert _ids(a_session, "secrets") == {world.a_secret.id}
    assert _ids(a_session, "secret_access_log") == set()
    listed = secrets_service.list_secrets(a_session, tenant_id=a, after=None, limit=100)
    assert [m.id for m in listed] == [world.a_secret.id]
    for tenant in (a, b, None):  # naming B (or the installation) as the owner does not help
        assert secrets_service.get_secret(a_session, world.b_secret.id, tenant_id=tenant) is None
    assert secrets_service.list_secrets(a_session, tenant_id=b, after=None, limit=100) == []
    assert secrets_service.list_secrets(a_session, tenant_id=None, after=None, limit=100) == []


def test_the_service_key_holds_without_row_level_security(
    owner_db: Database, keyring: KeyRing, world: World
) -> None:
    """The owner bypasses RLS: the service's own tenant key still hides B's secret from A."""
    a = world.clients.a
    with owner_db.session() as o:
        set_tenant(o, a.tenant)
        assert secrets_service.get_secret(o, world.b_secret.id, tenant_id=a.tenant) is None
        listed = secrets_service.list_secrets(o, tenant_id=a.tenant, after=None, limit=100)
        assert [m.id for m in listed] == [world.a_secret.id]
        c = caller(o, keyring, a.tenant, a.admin)
        with pytest.raises(AppError, match="secret not found"):
            c.provider.resolve(world.b_secret.id, c.principal, "steal", pdp=c.pdp)
        with pytest.raises(AppError, match="secret not found"):
            c.provider.resolve_system(world.b_secret.id, "steal", tenant_id=a.tenant)
        o.rollback()


def test_nothing_is_visible_without_a_tenant_context(app_db: Database, world: World) -> None:
    with app_session(app_db, None) as s:
        assert _ids(s, "secrets") == set() and _ids(s, "secret_access_log") == set()
        for tenant in (world.clients.a.tenant, world.clients.b.tenant, None):
            assert secrets_service.list_secrets(s, tenant_id=tenant, after=None, limit=100) == []
        with pytest.raises(DBAPIError) as err:
            s.execute(
                text(
                    "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
                    "VALUES (gen_random_uuid(), :t, 'x', 'env', 'X')"
                ),
                {"t": world.clients.a.tenant},
            )
        assert _sqlstate(err) == "42501"


def test_the_platform_admin_flag_reaches_installation_rows_only(
    app_db: Database, world: World
) -> None:
    with app_session(app_db, None) as s, platform_admin_context(s):
        assert _ids(s, "secrets") == {world.installation.id}
        assert _ids(s, "secret_access_log") == set()
        for tenant in (world.clients.a.tenant, world.clients.b.tenant):
            assert secrets_service.list_secrets(s, tenant_id=tenant, after=None, limit=100) == []


# ---------------------------------------------------------------- use
def test_a_cannot_use_bs_secret_and_the_refusal_matches_a_missing_id(
    a_session: Session, owner_db: Database, keyring: KeyRing, world: World
) -> None:
    before = _b_row(owner_db, world)
    c = caller(a_session, keyring, world.clients.a.tenant, world.clients.a.admin)
    for ref in (world.b_secret.id, world.installation.id):
        assert _refusal(
            lambda r=ref: c.provider.resolve(r, c.principal, "steal", pdp=c.pdp)
        ) == _refusal(lambda: c.provider.resolve(uuid7(), c.principal, "steal", pdp=c.pdp))
    system = SecretProvider(a_session, keyring, RequestContext(request_id="system"))
    for tenant in (world.clients.a.tenant, world.clients.b.tenant, None):
        assert _refusal(
            lambda t=tenant: system.resolve_system(world.b_secret.id, "steal", tenant_id=t)
        ) == _refusal(lambda t=tenant: system.resolve_system(uuid7(), "steal", tenant_id=t))
    a_session.commit()
    assert _b_row(owner_db, world) == before  # last_used_at untouched
    with owner_db.session() as o:
        rows = o.execute(text("SELECT tenant_id FROM shifde.secret_access_log")).scalars().all()
        assert rows == [world.clients.b.tenant]  # only B's own use


def test_a_pdp_for_a_refuses_bs_secret_even_in_bs_database_context(
    app_db: Database, keyring: KeyRing, world: World
) -> None:
    """A session whose context is B (a mis-set context) with A's principal and A's PDP: the PDP
    refuses the cross-tenant resource and hides it."""
    with app_session(app_db, world.clients.b.tenant) as s:
        c = caller(s, keyring, world.clients.a.tenant, world.clients.a.admin)
        with pytest.raises(AppError, match="not found"):
            c.provider.resolve(world.b_secret.id, c.principal, "steal", pdp=c.pdp)


def test_bs_envelope_copied_into_as_row_does_not_decrypt(
    a_session: Session, owner_db: Database, keyring: KeyRing, world: World
) -> None:
    with owner_db.session() as o:
        o.execute(
            text(
                "UPDATE shifde.secrets SET ciphertext = b.ciphertext, nonce = b.nonce, "
                "wrapped_dek = b.wrapped_dek, kek_id = b.kek_id FROM shifde.secrets b "
                "WHERE b.id = :b AND shifde.secrets.id = :a"
            ),
            {"a": world.a_secret.id, "b": world.b_secret.id},
        )
        o.commit()
    c = caller(a_session, keyring, world.clients.a.tenant, world.clients.a.admin)
    with pytest.raises(AppError, match="could not be decrypted"):  # AAD binds tenant and id
        c.provider.resolve(world.a_secret.id, c.principal, "x", pdp=c.pdp)


# ---------------------------------------------------------------- rotate, disable, overwrite
def test_a_cannot_rotate_or_disable_bs_secret(
    a_session: Session, owner_db: Database, keyring: KeyRing, world: World
) -> None:
    before = _b_row(owner_db, world)
    a = world.clients.a.tenant
    missing = replace(world.b_secret, id=uuid7())
    for target in (world.b_secret, replace(world.b_secret, tenant_id=a)):
        for version in (None, 1):
            assert _refusal(
                lambda t=target, v=version: secrets_service.rotate_secret(
                    a_session, keyring, t, value="stolen-value-1", expected_version=v
                )
            ) == _refusal(
                lambda v=version: secrets_service.rotate_secret(
                    a_session, keyring, missing, value="stolen-value-1", expected_version=v
                )
            )
            assert _refusal(
                lambda t=target, v=version: secrets_service.disable_secret(
                    a_session, t, expected_version=v
                )
            ) == _refusal(
                lambda v=version: secrets_service.disable_secret(
                    a_session, missing, expected_version=v
                )
            )
    a_session.commit()
    assert _b_row(owner_db, world) == before


def test_raw_sql_in_as_context_cannot_touch_bs_rows(a_session: Session, world: World) -> None:
    b_id, b = world.b_secret.id, world.clients.b.tenant
    updated = a_session.execute(
        text("UPDATE shifde.secrets SET status = 'disabled', ciphertext = '\\x00' WHERE id = :i"),
        {"i": b_id},
    )
    assert updated.rowcount == 0
    a_session.rollback()
    statements = [
        # write a row for B, or an installation-level row, from A's context
        (
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
            "VALUES (gen_random_uuid(), :b, 'planted', 'env', 'X')"
        ),
        (
            "INSERT INTO shifde.secrets (id, tenant_id, name, provider_key, external_ref) "
            "VALUES (gen_random_uuid(), NULL, 'planted', 'env', 'X')"
        ),
        (
            "INSERT INTO shifde.secret_access_log (id, tenant_id, secret_id, purpose, outcome) "
            "VALUES (gen_random_uuid(), :b, :s, 'p', 'success')"
        ),
        # no DELETE or TRUNCATE grant, no UPDATE of the owning tenant or the name
        "DELETE FROM shifde.secrets WHERE id = :s",
        "TRUNCATE shifde.secrets CASCADE",
        "UPDATE shifde.secrets SET tenant_id = :b WHERE id = :s",
        "UPDATE shifde.secrets SET name = 'renamed' WHERE id = :s",
    ]
    for stmt in statements:
        with pytest.raises(DBAPIError) as err:
            a_session.execute(text(stmt), {"b": b, "s": b_id})
        assert _sqlstate(err) == "42501", stmt
        a_session.rollback()


def test_as_access_log_cannot_name_bs_secret(a_session: Session, world: World) -> None:
    """The composite key (tenant_id, secret_id) -> secrets (tenant_id, id) refuses a row of A
    naming B's secret, which SETH's plain key accepted (constraints bypass RLS; B-801)."""
    with pytest.raises(DBAPIError) as err:
        a_session.execute(
            text(
                "INSERT INTO shifde.secret_access_log (id, tenant_id, secret_id, purpose, outcome) "
                "VALUES (gen_random_uuid(), :a, :s, 'p', 'success')"
            ),
            {"a": world.clients.a.tenant, "s": world.b_secret.id},
        )
    assert _sqlstate(err) == "23503"
    assert "fk_secret_access_log__secret" in str(err.value)
