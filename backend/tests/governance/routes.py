"""governance route harness: the identity harness app plus `governance.api.router` behind
`require_auth`, and the SETH `p8util` rate-card helpers. The `app` port mounts the router (B-501).
People and clients are the tenancy route-test world."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JSON bodies, TestClient options
from uuid import UUID

from fastapi import Depends
from fastapi.testclient import TestClient

from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.governance import api
from shifde.identity.dependencies import require_auth
from tests.connections.routes import post
from tests.identity import harness
from tests.tenancy.conftest import Client

PLATFORM = "/api/v1/platform"
CURRENCY = "XTS"  # ISO 4217 code reserved for testing: never a real currency
PAST = "2000-01-01T00:00:00Z"
IN_RATE = 1000.0
OUT_RATE = 2000.0


@contextmanager
def governance_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


def rate_card(
    client: TestClient,
    h: dict[str, str],
    base: str,
    model_key: str = "chat-a",
    provider_key: str = "fake",
) -> dict[str, Any]:
    """SETH `p8util.rate_card`."""
    out: dict[str, Any] = post(
        client,
        f"{base}/rate-cards",
        h,
        {"provider_key": provider_key, "model_key": model_key, "name": f"TEST {model_key}"},
    )
    return out


def rate_version(
    client: TestClient,
    h: dict[str, str],
    base: str,
    card_id: str,
    *,
    input_rate: float = IN_RATE,
    output_rate: float = OUT_RATE,
    effective_from: str = PAST,
    effective_to: str | None = None,
    status: int = 201,
    **extra: Any,
) -> dict[str, Any]:
    """SETH `p8util.rate_version`."""
    body: dict[str, Any] = {
        "effective_from": effective_from,
        "currency": CURRENCY,
        "unit": "per_1m_tokens",
        "input_rate": input_rate,
        "output_rate": output_rate,
        "source": "TEST rate card - not vendor pricing",
        "revision": "t-1",
        **extra,
    }
    if effective_to:
        body["effective_to"] = effective_to
    out: dict[str, Any] = post(client, f"{base}/rate-cards/{card_id}/versions", h, body, status)
    return out


def tenant_scope(c: Client) -> dict[str, str]:
    """SETH `p4util.tenant_scope`, for one client of the tenancy world."""
    return {"type": "tenant", "id": str(c.tenant)}


def project_scope(c: Client) -> dict[str, str]:
    """SETH `p8util.project_scope`: the client's project."""
    return {"type": "project", "id": str(c.project)}


def workspace_scope(workspace: UUID) -> dict[str, str]:
    return {"type": "workspace", "id": str(workspace)}


def budget(
    client: TestClient,
    h: dict[str, str],
    base: str,
    scope: dict[str, str],
    *,
    metric: str = "tokens",
    amount: float = 100000,
    period: str = "daily",
    status: int = 201,
    **extra: Any,
) -> dict[str, Any]:
    """SETH `p8util.budget` (`base` is the organization path instead of the module constant)."""
    body: dict[str, Any] = {
        "scope": scope,
        "period": period,
        "amount": amount,
        "metric": metric,
        **({"currency": CURRENCY} if metric == "cost" else {}),
        **extra,
    }
    out: dict[str, Any] = post(client, f"{base}/budgets", h, body, status)
    return out
