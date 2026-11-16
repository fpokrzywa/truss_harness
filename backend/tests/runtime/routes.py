"""Runtime part 12 (RT-12): the runtime routes over a minimal FastAPI app (B-1020 pattern).

The app is the identity harness plus `runtime.api.router` behind `require_auth`, with the
integration runtime (pipeline schedules turned on) and the governance hooks SETH's app puts on
`app.state`; the `app` port mounts the router (APP-2). The client comes only from the path's
organization (`tenancy.dependencies.resolve_org`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any  # Any: TestClient options

from fastapi import Depends
from fastapi.testclient import TestClient

from shifde.adapters.runtime import IntegrationRuntime
from shifde.core.deps import check_origin, reject_basic_auth
from shifde.core.settings import Settings
from shifde.identity.dependencies import require_auth
from shifde.runtime import api
from shifde.runtime.governance import PolicyBudgetHooks
from tests.connections.routes import REGISTRY
from tests.identity import harness


@contextmanager
def runtime_client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    on = settings.model_copy(update={"feature_pipeline_schedules": True})
    app.state.integration = IntegrationRuntime(settings=on, registry=REGISTRY)
    app.state.governance = PolicyBudgetHooks()
    guards = [Depends(reject_basic_auth), Depends(check_origin), Depends(require_auth)]
    app.include_router(api.router, dependencies=guards)
    try:
        with TestClient(
            app, base_url="https://testserver", headers={"Origin": harness.ORIGIN}, **kwargs
        ) as c:
            yield c
    finally:
        app.state.db.dispose()
