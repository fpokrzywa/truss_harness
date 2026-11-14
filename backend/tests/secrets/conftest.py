"""Fixtures for the secrets tests.

Two clients (A, B), each with a user bound to `tenant_administrator` (holds `secret:use`), plus
users bound to `auditor` (`secret:read`, no `secret:use`) and to nothing. Until identity and
tenancy are ported there is no seeded world: ids are fresh, and bindings go in through
`tests.authz.conftest.seed_bindings`, which creates each person (with a membership) first and
inserts with the row triggers active.

`secret_access_log` is append-only (row triggers refuse UPDATE/DELETE) but the per-test cleanup
TRUNCATEs as the owner, which row triggers do not see, so both tables are registered.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shifde.authz.service import PDP, new_pdp
from shifde.core.context import Principal, RequestContext
from shifde.core.crypto import KeyRing
from shifde.core.db import Database, set_principal, set_tenant
from shifde.core.ids import uuid7
from shifde.secrets.service import SecretProvider
from tests._support.tenants import add_tenants
from tests.authz.conftest import seed_bindings
from tests.conftest import CLEAN_TABLES

for _table in ("secret_access_log", "secrets", "audit_events"):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@dataclass(frozen=True)
class Client:
    tenant: UUID
    admin: UUID  # tenant_administrator: secret:read, secret:manage, secret:use
    auditor: UUID  # auditor: secret:read, no secret:use
    nobody: UUID  # no binding at all


@dataclass(frozen=True)
class Clients:
    a: Client
    b: Client


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


@pytest.fixture
def seed_engine(worker_admin_url: str) -> Iterator[Engine]:
    engine = create_engine(worker_admin_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def keyring() -> KeyRing:
    return KeyRing.parse(KeyRing.generate_entry("k1"))


def _client(seed: Engine) -> Client:
    tenant, admin, auditor, nobody = uuid7(), uuid7(), uuid7(), uuid7()
    with seed.begin() as c:
        add_tenants(c, tenant)
    seed_bindings(
        seed,
        [
            {
                "tenant_id": tenant,
                "subject_id": uid,
                "role_key": role,
                "scope_type": "tenant",
                "scope_id": tenant,
            }
            for uid, role in ((admin, "tenant_administrator"), (auditor, "auditor"))
        ],
    )
    return Client(tenant, admin, auditor, nobody)


@pytest.fixture
def clients(seed_engine: Engine) -> Clients:
    return Clients(_client(seed_engine), _client(seed_engine))


def app_session(db: Database, tenant: UUID | None, principal: UUID | None = None) -> Session:
    """A `shifde_app` session in `tenant`'s context (none if `tenant` is None)."""
    s = db.session()
    if tenant is not None:
        set_tenant(s, tenant)
    if principal is not None:
        set_principal(s, principal)
    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    return s


@dataclass
class Caller:
    session: Session
    principal: Principal
    pdp: PDP
    provider: SecretProvider


def caller(s: Session, keyring: KeyRing, tenant: UUID, user: UUID) -> Caller:
    """`user` of `tenant` on session `s` (SETH's `_provider_env`, without the app)."""
    principal = Principal(user, f"{user}@example.test", "user")
    ctx = RequestContext(request_id="secrets-test", principal=principal)
    return Caller(s, principal, new_pdp(s, principal, tenant), SecretProvider(s, keyring, ctx))
