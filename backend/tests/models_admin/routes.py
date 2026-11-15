"""models_admin part 2a: the provider, model and SHI routes over a minimal FastAPI app (B-1020).

The app is the connections route harness (`tests/connections/routes.py`: identity harness, the
integration runtime) plus `models_admin.api.router` behind `require_auth`; the `app` port mounts the
router (B-501). People and clients are the tenancy route-test world.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: JSON bodies, TestClient options

from fastapi import Depends
from fastapi.testclient import TestClient

from shifde.adapters.runtime import IntegrationRuntime
from shifde.connections import api as connections_api
from shifde.core.db import Database, platform_admin_context
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.identity.dependencies import require_auth
from shifde.models_admin import api, repository
from tests.connections.routes import REGISTRY, activate, as_user, connection, post
from tests.identity import harness
from tests.models_admin.conftest import model_values
from tests.tenancy.conftest import app_session

PLATFORM = "/api/v1/platform"


@contextmanager
def models_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    app.state.integration = IntegrationRuntime(settings=settings, registry=REGISTRY)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(connections_api.router, dependencies=guards)
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()


def fake_model_stack(
    client: TestClient,
    h: dict[str, str],
    base: str,
    *,
    model_key: str = "fake-chat",
    lifecycle: str = "active",
    conn_name: str = "fake-conn",
    vendor_id: str | None = None,
) -> dict[str, str]:
    """SETH `p3util.fake_model_stack`: active fake connection -> provider -> model; their ids."""
    conn = connection(
        client,
        h,
        {"name": conn_name, "connection_type": "model_api", "adapter_key": "fake", "config": {}},
        base,
    )
    activate(client, h, conn["id"], base)
    body: dict[str, Any] = {
        "name": f"prov-{conn_name}",
        "adapter_key": "fake",
        "connection_id": conn["id"],
    }
    if vendor_id is not None:
        body["vendor_id"] = vendor_id
    provider = post(client, f"{base}/model-providers", h, body)
    model = post(
        client,
        f"{base}/models",
        h,
        {
            "provider_id": provider["id"],
            "model_key": model_key,
            "display_name": model_key.title(),
            "capabilities": ["chat"],
            "context_window": 8192,
            "max_output_tokens": 1024,
            "lifecycle": lifecycle,
        },
    )
    return {"connection": conn["id"], "provider": provider["id"], "model": model["id"]}


def shared_model(app_db: Database, model_key: str = "shi-model") -> str:
    """A model of the SHI provider, written in the platform administrator context (no client
    route writes one; the platform model registry arrives with part 2b)."""
    with app_session(app_db, None) as s, platform_admin_context(s):
        values = model_values(None, repository.SHI_PROVIDER_ID, model_key=model_key)
        repository.insert_model(s, values)
        s.commit()
    return str(values["id"])


def switch(client: TestClient, platform: str, slug: str, enabled: bool) -> Any:  # Any: JSON
    h = as_user(client, platform)
    r = client.put(
        f"{PLATFORM}/orgs/{slug}/shi-provider-access", headers=h, json={"enabled": enabled}
    )
    assert r.status_code == 200, r.text
    return r.json()
