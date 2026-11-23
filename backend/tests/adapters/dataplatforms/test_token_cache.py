"""The OAuth M2M token cache of the `databricks` adapter (CM-3, issue #161; design 2026-11-22
section 7.2): an entry per (client, connection, secret fingerprint), so another client, another
connection or a rotated secret never reuses a token; every token is registered for redaction.
The `snowflake` key-pair JWT (CM-4, issue #162; section 7.3) is cached the same way, under
(client, connection, sha256(key, account, user))."""

from __future__ import annotations

import itertools
from uuid import UUID

import pytest

from shifde.adapters import oauth2
from shifde.adapters.dataplatforms import snowflake
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace, FakeSnowflakeAccount
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from shifde.adapters.oauth2 import AUTH_METHOD as OAUTH2
from shifde.core.ids import uuid7
from shifde.core.redaction import REDACTED, scrub_text
from tests.adapters.dataplatforms.helpers import (
    CLIENT_SECRET,
    SF_PEM,
    conn,
    ctx_for,
    pkcs8,
    rsa_key,
    sf_conn,
)

ADAPTER = DatabricksAdapter()
ME = "/api/2.0/preview/scim/v2/Me"


def _used_token(
    fake: FakeDatabricksWorkspace, tenant: UUID, connection_id: UUID, secret: str = CLIENT_SECRET
) -> str:
    """Run a connection test; the bearer token its `Me` call carried."""
    out = ADAPTER.test(
        ctx_for(fake, tenant=tenant), conn(OAUTH2, secret, connection_id=connection_id)
    )
    assert out.passed, out.checks
    me = [r for r in fake.requests if r.url.path == ME][-1]
    return me.headers["authorization"].removeprefix("Bearer ")


def _fetches(fake: FakeDatabricksWorkspace) -> int:
    return sum(1 for r in fake.requests if r.url.path == "/oidc/v1/token")


def test_two_clients_never_share_a_token(fake: FakeDatabricksWorkspace) -> None:
    same_connection = uuid7()  # the same connection id, host, client_id and secret for both
    a = _used_token(fake, uuid7(), same_connection)
    b = _used_token(fake, uuid7(), same_connection)
    assert a != b
    assert _fetches(fake) == 2 and len(oauth2.TOKEN_CACHE) == 2


def test_two_connections_of_one_client_never_share_a_token(fake: FakeDatabricksWorkspace) -> None:
    tenant = uuid7()
    first, second = uuid7(), uuid7()
    a = _used_token(fake, tenant, first)
    b = _used_token(fake, tenant, second)
    assert a != b and _fetches(fake) == 2 and len(oauth2.TOKEN_CACHE) == 2
    assert _used_token(fake, tenant, first) == a  # the cached entry is reused
    assert _fetches(fake) == 2


def test_a_rotated_secret_fetches_a_new_token(fake: FakeDatabricksWorkspace) -> None:
    fake.client_secret = None  # accept any secret: the rotation itself is what is tested
    tenant, connection_id = uuid7(), uuid7()
    before = _used_token(fake, tenant, connection_id, "dbx-secret-before-0001")
    after = _used_token(fake, tenant, connection_id, "dbx-secret-after-0002")
    assert before != after and _fetches(fake) == 2


def test_a_refused_token_is_refreshed_once(fake: FakeDatabricksWorkspace) -> None:
    tenant, connection_id = uuid7(), uuid7()
    first = _used_token(fake, tenant, connection_id)
    fake.issued.remove(first)  # the workspace revokes it; the cache still holds it
    second = _used_token(fake, tenant, connection_id)
    assert second != first and _fetches(fake) == 2


def test_tokens_are_registered_for_redaction(fake: FakeDatabricksWorkspace) -> None:
    token = _used_token(fake, uuid7(), uuid7())
    assert token in fake.issued
    assert scrub_text(f"leaked {token} here") == f"leaked {REDACTED} here"
    assert token not in repr(oauth2.TOKEN_CACHE)


# ------------------------------------------------------------------ Snowflake key-pair JWT (CM-4)
SNOWFLAKE = SnowflakeAdapter()


@pytest.fixture(autouse=True)
def _ticking_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every JWT is signed one second after the previous one: two signatures of the same claims
    would otherwise be byte-identical (RS256 is deterministic), and sharing would not show."""
    ticks = itertools.count(1_900_000_000)
    monkeypatch.setattr(snowflake, "clock", lambda: float(next(ticks)))


def _jwt_used(
    sf: FakeSnowflakeAccount, tenant: UUID | None, connection_id: UUID | None, pem: str = SF_PEM
) -> str:
    """Run a connection test; the JWT its statement call carried."""
    sf.clock = lambda: 1_800_000_000.0  # before every ticked `iat`: no JWT is expired
    out = SNOWFLAKE.test(
        ctx_for(sf, tenant=tenant), sf_conn(secret=pem, connection_id=connection_id)
    )
    assert out.passed, out.checks
    return sf.requests[-1].headers["authorization"].removeprefix("Bearer ")


def test_two_clients_never_share_a_jwt(sf: FakeSnowflakeAccount) -> None:
    same_connection = uuid7()  # the same connection id, account, user and key for both
    a = _jwt_used(sf, uuid7(), same_connection)
    b = _jwt_used(sf, uuid7(), same_connection)
    assert a != b and len(oauth2.TOKEN_CACHE) == 2
    keys = list(oauth2.TOKEN_CACHE._entries)
    assert keys[0][0] != keys[1][0] and keys[0][1:] == keys[1][1:]


def test_two_connections_of_one_client_never_share_a_jwt(sf: FakeSnowflakeAccount) -> None:
    tenant = uuid7()
    first, second = uuid7(), uuid7()
    a = _jwt_used(sf, tenant, first)
    b = _jwt_used(sf, tenant, second)
    assert a != b and len(oauth2.TOKEN_CACHE) == 2
    assert _jwt_used(sf, tenant, first) == a  # the cached entry is reused
    assert _jwt_used(sf, tenant, second) == b


def test_a_planted_entry_of_one_client_never_reaches_another(sf: FakeSnowflakeAccount) -> None:
    tenant_a, tenant_b, connection_id = uuid7(), uuid7(), uuid7()
    _jwt_used(sf, tenant_a, connection_id)
    (key_a,) = list(oauth2.TOKEN_CACHE._entries)
    oauth2.TOKEN_CACHE.put(key_a, "tenant-a-only.jwt.value", oauth2.clock() + 100)
    b = _jwt_used(sf, tenant_b, connection_id)
    assert "tenant-a-only.jwt.value" not in [
        r.headers["authorization"].removeprefix("Bearer ") for r in sf.requests
    ]
    assert b != "tenant-a-only.jwt.value"


def test_a_rotated_key_gives_a_new_fingerprint_and_jwt(sf: FakeSnowflakeAccount) -> None:
    tenant, connection_id = uuid7(), uuid7()
    before = _jwt_used(sf, tenant, connection_id)
    sf.public_key = rsa_key("rotated").public_key()
    after = _jwt_used(sf, tenant, connection_id, pkcs8(rsa_key("rotated")))
    assert before != after and len(oauth2.TOKEN_CACHE) == 2
    fingerprints = {key[2] for key in oauth2.TOKEN_CACHE._entries}
    assert len(fingerprints) == 2
    assert before.split(".")[1] != after.split(".")[1]  # another `iss` fingerprint too


def test_no_entry_without_a_client_or_a_connection(sf: FakeSnowflakeAccount) -> None:
    _jwt_used(sf, None, uuid7())
    _jwt_used(sf, uuid7(), None)
    _jwt_used(sf, None, None)
    assert len(oauth2.TOKEN_CACHE) == 0


def test_jwts_are_registered_for_redaction(sf: FakeSnowflakeAccount) -> None:
    token = _jwt_used(sf, uuid7(), uuid7())
    assert scrub_text(f"leaked {token} here") == f"leaked {REDACTED} here"
    assert token not in repr(oauth2.TOKEN_CACHE)
