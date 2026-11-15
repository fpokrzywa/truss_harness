"""ontology part 2: the ontology routes over a minimal FastAPI app (B-1020 pattern, B-2007).

The build route harness (connections, models_admin and build routers behind `require_auth`) plus
`ontology.api.router` behind the same guards; the `app` port mounts the router (B-501).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: TestClient options

from fastapi import Depends
from fastapi.testclient import TestClient

from shifde.adapters.runtime import IntegrationRuntime
from shifde.build import api as build_api
from shifde.connections import api as connections_api
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.identity.dependencies import require_auth
from shifde.models_admin import api as models_api
from shifde.ontology import api
from tests.connections.routes import REGISTRY
from tests.identity import harness


@contextmanager
def ontology_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    app.state.integration = IntegrationRuntime(settings=settings, registry=REGISTRY)
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    for router in (connections_api.router, models_api.router, build_api.router, api.router):
        app.include_router(router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()
