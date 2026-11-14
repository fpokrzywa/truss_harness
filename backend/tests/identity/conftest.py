"""Fixtures for the identity tests (part 1: tables, repository, passwords).

Two clients (A, B) and their people, seeded as the schema owner (`shifde_owner`, BYPASSRLS):
users are platform-level (one row per person), memberships, service accounts, API keys and IdP
configurations belong to one client. Tenancy is not ported, so tenant ids are fresh UUIDs (the
`tenants` foreign keys are deferred to tenancy, docs/build/carry_forward.md).

`app_session` opens a `shifde_app` session and asserts `current_user` (B-116 item 12). The
`app_db` pool holds one connection: close a session before opening the next.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database, set_principal, set_tenant
from shifde.core.ids import uuid7
from tests.conftest import CLEAN_TABLES

# children first is not needed: the cleanup also truncates every table that references these
for _table in (
    "user_identities",
    "api_keys",
    "service_accounts",
    "setup_tokens",
    "sessions",
    "local_credentials",
    "memberships",
    "idp_configs",
    "users",
    "secret_access_log",
    "secrets",
):
    if _table not in CLEAN_TABLES:
        CLEAN_TABLES.append(_table)


@dataclass(frozen=True)
class Client:
    tenant: UUID
    member: UUID  # a person with a membership in this client only
    membership: UUID
    service_account: UUID
    api_key: UUID  # the service account's key
    user_key: UUID  # the member's key
    idp: UUID  # this client's IdP configuration
    session_hash: bytes  # the member's login session


@dataclass(frozen=True)
class World:
    a: Client
    b: Client
    both: UUID  # a person who is a member of A and of B
    installation_idp: UUID  # tenant_id NULL


@pytest.fixture
def owner_session(owner_db: Database) -> Iterator[Session]:
    with owner_db.session() as s:
        yield s


def app_session(db: Database, tenant: UUID | None, principal: UUID | None = None) -> Session:
    """A `shifde_app` session in `tenant`'s context (none if None), acting as `principal`."""
    s = db.session()
    if tenant is not None:
        set_tenant(s, tenant)
    if principal is not None:
        set_principal(s, principal)
    assert s.execute(text("SELECT current_user")).scalar_one() == "shifde_app"
    return s


def owner_user(owner: Session, email: str, *, admin: bool = False) -> UUID:
    uid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.users (id, primary_email, display_name, is_platform_admin) "
            "VALUES (:id, :e, :e, :a)"
        ),
        {"id": uid, "e": email, "a": admin},
    )
    return uid


def owner_membership(owner: Session, tenant: UUID, user: UUID) -> UUID:
    mid = uuid7()
    owner.execute(
        text("INSERT INTO shifde.memberships (id, tenant_id, user_id) VALUES (:id, :t, :u)"),
        {"id": mid, "t": tenant, "u": user},
    )
    return mid


def owner_api_key(
    owner: Session, tenant: UUID, principal_type: str, principal: UUID, prefix: str
) -> UUID:
    kid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.api_keys (id, tenant_id, name, principal_type, principal_id, "
            "prefix, key_hash, scopes, expires_at) VALUES (:id, :t, :n, :pt, :p, :pre, :h, "
            "ARRAY['read'], :exp)"
        ),
        {
            "id": kid,
            "t": tenant,
            "n": f"key-{prefix}",
            "pt": principal_type,
            "p": principal,
            "pre": prefix,
            "h": prefix.encode() * 4,
            "exp": utcnow() + timedelta(days=30),
        },
    )
    return kid


def owner_idp(owner: Session, tenant: UUID | None, issuer: str) -> UUID:
    iid = uuid7()
    owner.execute(
        text(
            "INSERT INTO shifde.idp_configs (id, tenant_id, display_name, issuer, discovery_url, "
            "client_id) VALUES (:id, :t, :n, :i, :d, 'client')"
        ),
        {"id": iid, "t": tenant, "n": issuer, "i": issuer, "d": f"{issuer}/.well-known"},
    )
    return iid


def owner_login_session(owner: Session, user: UUID, session_hash: bytes) -> UUID:
    sid = uuid7()
    now = utcnow()
    owner.execute(
        text(
            "INSERT INTO shifde.sessions (id, session_hash, user_id, csrf_hash, idle_expires_at, "
            "absolute_expires_at) VALUES (:id, :h, :u, :c, :i, :a)"
        ),
        {
            "id": sid,
            "h": session_hash,
            "u": user,
            "c": b"csrf-" + session_hash,
            "i": now + timedelta(hours=1),
            "a": now + timedelta(hours=8),
        },
    )
    return sid


def _client(owner: Session, name: str) -> Client:
    tenant = uuid7()
    member = owner_user(owner, f"member@{name}.test")
    membership = owner_membership(owner, tenant, member)
    sa = uuid7()
    owner.execute(
        text("INSERT INTO shifde.service_accounts (id, tenant_id, name) VALUES (:id, :t, :n)"),
        {"id": sa, "t": tenant, "n": "same-name"},
    )
    api_key = owner_api_key(owner, tenant, "service_account", sa, f"{name}sa_key0"[:8])
    user_key = owner_api_key(owner, tenant, "user", member, f"{name}us_key0"[:8])
    idp = owner_idp(owner, tenant, f"https://idp.{name}.test")
    owner.execute(
        text(
            "INSERT INTO shifde.local_credentials (user_id, password_hash) VALUES (:u, 'hash-' || :n)"
        ),
        {"u": member, "n": name},
    )
    session_hash = f"session-{name}".encode()
    owner_login_session(owner, member, session_hash)
    owner.execute(
        text(
            "INSERT INTO shifde.user_identities (id, user_id, issuer, subject, idp_config_id) "
            "VALUES (:id, :u, :i, :s, :c)"
        ),
        {"id": uuid7(), "u": member, "i": f"https://idp.{name}.test", "s": name, "c": idp},
    )
    return Client(tenant, member, membership, sa, api_key, user_key, idp, session_hash)


@pytest.fixture
def world(owner_session: Session) -> World:
    a = _client(owner_session, "a")
    b = _client(owner_session, "b")
    both = owner_user(owner_session, "both@shi.test")
    owner_membership(owner_session, a.tenant, both)
    owner_membership(owner_session, b.tenant, both)
    installation = owner_idp(owner_session, None, "https://login.shi.test")
    owner_session.commit()
    return World(a, b, both, installation)
