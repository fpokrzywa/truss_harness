"""Helpers of the data-platform adapter tests (CM-3, issue #161): the in-process Databricks fake,
an egress guard that reaches it (`resolver` loopback, `inner` the fake's transport, behind the
development/test installation layer unless a test says otherwise) and ExecContext /
ConnectionConfig helpers. No test here opens a socket or imports a vendor SDK."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any  # Any: config values

from shifde.adapters import oauth2
from shifde.adapters.base import ConnectionConfig, Credential, ExecContext
from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace
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


def guard_for(
    fake: FakeDatabricksWorkspace,
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
    fake: FakeDatabricksWorkspace, tenant: Any = None, **guard: Any
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
