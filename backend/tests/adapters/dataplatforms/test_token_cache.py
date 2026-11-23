"""The OAuth M2M token cache of the `databricks` adapter (CM-3, issue #161; design 2026-11-22
section 7.2): an entry per (client, connection, secret fingerprint), so another client, another
connection or a rotated secret never reuses a token; every token is registered for redaction."""

from __future__ import annotations

from uuid import UUID

from shifde.adapters import oauth2
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace
from shifde.adapters.oauth2 import AUTH_METHOD as OAUTH2
from shifde.core.ids import uuid7
from shifde.core.redaction import REDACTED, scrub_text
from tests.adapters.dataplatforms.helpers import CLIENT_SECRET, conn, ctx_for

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
