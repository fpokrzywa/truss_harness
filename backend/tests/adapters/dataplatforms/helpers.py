"""Helpers of the data-platform adapter tests (CM-3, issue #161; CM-4, issue #162): the in-process
Databricks and Snowflake fakes,
an egress guard that reaches it (`resolver` loopback, `inner` the fake's transport, behind the
development/test installation layer unless a test says otherwise) and ExecContext /
ConnectionConfig helpers. No test here opens a socket or imports a vendor SDK."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Any  # Any: config values

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shifde.adapters import oauth2
from shifde.adapters.base import ConnectionConfig, Credential, ExecContext
from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace, FakeSnowflakeAccount
from shifde.core.egress import EgressGuard, EgressPolicy, Resolver, installation_layer
from shifde.core.ids import uuid7

HOST = "ws1.cloud.databricks.com"
URL = f"https://{HOST}"
PAT = "dbx-test-pat-0001"  # scan-secrets: allow, the fake's accepted token
CLIENT_ID = "sp-client-0001"
CLIENT_SECRET = "dbx-test-client-secret-0001"  # scan-secrets: allow, the fake's OAuth secret
WAREHOUSE = "wh-0001"


def make_fake() -> FakeDatabricksWorkspace:
    return FakeDatabricksWorkspace(
        host=HOST,
        token=PAT,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        warehouse_id=WAREHOUSE,
    )


Fake = FakeDatabricksWorkspace | FakeSnowflakeAccount


def guard_for(
    fake: Fake,
    *,
    env: str = "test",
    layers: Sequence[EgressPolicy] = (),
    resolver: Resolver | None = None,
    deadline: float | None = None,
) -> EgressGuard:
    guard = EgressGuard(
        [installation_layer(None, env=env), *layers],
        resolver=resolver or (lambda host: ["127.0.0.1"]),
        inner=fake.transport(),
    )
    if deadline is not None:
        guard.set_deadline(deadline)
    return guard


def ctx_for(
    fake: Fake, tenant: Any = None, **guard: Any
) -> ExecContext:  # Any: UUID | None, guard_for options
    return ExecContext(tenant_id=tenant, principal_id=None, egress=guard_for(fake, **guard))


def conn(
    auth: str = "bearer",
    secret: str | None = PAT,
    *,
    connection_id: Any = None,  # Any: UUID | None
    **config: Any,
) -> ConnectionConfig:
    cfg: dict[str, Any] = {"host": URL, "warehouse_id": WAREHOUSE, **config}
    if auth == oauth2.AUTH_METHOD:
        cfg.setdefault("client_id", CLIENT_ID)
    return ConnectionConfig(
        id=connection_id or uuid7(),
        name="lakehouse",
        adapter_key="databricks",
        config={k: v for k, v in cfg.items() if v is not None},
        auth_method=auth,
        credential=Credential(secret) if secret else None,
    )


def bearer_tokens(fake: FakeDatabricksWorkspace, path: str) -> list[str]:
    """The bearer token of every recorded request to `path`, in order."""
    return [
        r.headers["authorization"].removeprefix("Bearer ")
        for r in fake.requests
        if r.url.path == path and "authorization" in r.headers
    ]


# ------------------------------------------------------------------ Snowflake (CM-4)
SF_ACCOUNT = "acct1"
SF_USER = "reader"
SF_PAT = "sf-test-pat-0001"  # scan-secrets: allow, the fake's accepted token


@cache
def rsa_key(label: str = "main", bits: int = 2048) -> rsa.RSAPrivateKey:
    """An RSA key generated once per (label, bits) in this test process."""
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def pkcs8(key: Any) -> str:  # Any: a cryptography private key
    """The unencrypted PKCS#8 PEM of `key`."""
    return key.private_bytes(  # type: ignore[no-any-return]
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


SF_PEM = pkcs8(rsa_key())


def make_snowflake(**kwargs: Any) -> FakeSnowflakeAccount:  # Any: fake fields
    defaults: dict[str, Any] = {
        "account": SF_ACCOUNT,
        "user": SF_USER,
        "public_key": rsa_key().public_key(),
        "pat": SF_PAT,
    }
    return FakeSnowflakeAccount(**{**defaults, **kwargs})


def sf_conn(
    auth: str = "key_pair",
    secret: str | None = None,
    *,
    connection_id: Any = None,  # Any: UUID | None
    **config: Any,
) -> ConnectionConfig:
    value = secret if secret is not None else (SF_PEM if auth == "key_pair" else SF_PAT)
    cfg: dict[str, Any] = {"account": SF_ACCOUNT, "user": SF_USER, **config}
    return ConnectionConfig(
        id=connection_id,
        name="warehouse",
        adapter_key="snowflake",
        config={k: v for k, v in cfg.items() if v is not None},
        auth_method=auth,
        credential=Credential(value) if value else None,
    )
