"""Model provider and model routes (contract P3 section 3) and the SHI-wide provider's routes.

Ported from SETH 3ba0e06 `models_admin/api.py`: `/model-providers*`, `/models*` (part 2a) and
`/model-allowlists`, `/models/effective` (registered before `/models/{model_id}`),
`/models/{model_id}/invoke`, `/model-routes*` (part 2b, B-1731). The `app` port mounts `router`
(B-501).

* Every `{org}` route resolves the client through `tenancy.dependencies.resolve_org` (B-1019).
* `GET /orgs/{org}/shi-provider-access`: the client reads its own switch (`model:read`).
* `/platform/shi-provider` and `/platform/orgs/{org}/shi-provider-access`: the platform
  administrator only (design 3 and 5.3, DECISIONS B-1717): configures the shared provider and
  switches it on or off for a client by slug.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from shifde.adapters.deps import RuntimeDep
from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.identity.dependencies import AuthDep
from shifde.models_admin import schemas as s
from shifde.models_admin import service
from shifde.models_admin.schemas import ScopeType
from shifde.models_admin.service import ModelService
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")


def _service(scope: ScopeDep, runtime: RuntimeDep, request: Request) -> ModelService:
    # the request's database: an invoke writes its usage row in its own transaction (Item 11)
    return ModelService(scope, runtime, request.app.state.db)


Models = Annotated[ModelService, Depends(_service)]


# ---------------------------------------------------------------- providers
@router.get("/orgs/{org}/model-providers", tags=["models"])
def list_providers(
    svc: Models, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.ProviderOut]:
    return svc.list_providers(limit, cursor)


@router.post("/orgs/{org}/model-providers", status_code=201, tags=["models"])
def create_provider(svc: Models, body: s.ProviderCreate) -> s.ProviderOut:
    return svc.create_provider(body)


@router.get("/orgs/{org}/model-providers/{provider_id}", tags=["models"])
def get_provider(svc: Models, provider_id: UUID) -> s.ProviderOut:
    return svc.get_provider(provider_id)


@router.patch("/orgs/{org}/model-providers/{provider_id}", tags=["models"])
def update_provider(svc: Models, provider_id: UUID, body: s.ProviderPatch) -> s.ProviderOut:
    return svc.update_provider(provider_id, body)


@router.post(
    "/orgs/{org}/model-providers/{provider_id}/health-check", status_code=202, tags=["models"]
)
def health_check(svc: Models, provider_id: UUID) -> s.HealthCheckJobOut:
    return svc.enqueue_health_check(provider_id)


@router.get("/orgs/{org}/model-providers/{provider_id}/health-check-jobs/{job_id}", tags=["models"])
def get_health_check_job(svc: Models, provider_id: UUID, job_id: UUID) -> s.HealthCheckJobOut:
    return svc.health_check_job(provider_id, job_id)


@router.post(
    "/orgs/{org}/model-providers/{provider_id}/discover-models", status_code=202, tags=["models"]
)
def discover_models(svc: Models, provider_id: UUID) -> s.DiscoveryJobOut:
    return svc.enqueue_discovery(provider_id)


@router.get("/orgs/{org}/model-providers/{provider_id}/discovery-jobs/{job_id}", tags=["models"])
def get_discovery_job(svc: Models, provider_id: UUID, job_id: UUID) -> s.DiscoveryJobOut:
    return svc.discovery_job(provider_id, job_id)


# ---------------------------------------------------------------- models
@router.get("/orgs/{org}/models", tags=["models"])
def list_models(
    svc: Models,
    provider_id: Annotated[UUID | None, Query()] = None,
    lifecycle: Annotated[str | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.ModelOut]:
    return svc.list_models(provider_id=provider_id, lifecycle=lifecycle, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/models", status_code=201, tags=["models"])
def create_model(svc: Models, body: s.ModelCreate) -> s.ModelOut:
    return svc.create_model(body)


@router.get("/orgs/{org}/models/effective", tags=["models"])
def effective_models(
    svc: Models,
    workspace: Annotated[str | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.ModelOut]:
    return svc.effective_models(workspace, project, limit, cursor)


@router.get("/orgs/{org}/models/{model_id}", tags=["models"])
def get_model(svc: Models, model_id: UUID) -> s.ModelOut:
    return svc.get_model(model_id)


@router.patch("/orgs/{org}/models/{model_id}", tags=["models"])
def update_model(svc: Models, model_id: UUID, body: s.ModelPatch) -> s.ModelOut:
    return svc.update_model(model_id, body)


@router.post("/orgs/{org}/models/{model_id}/invoke", tags=["models"])
def invoke_model(
    svc: Models,
    model_id: UUID,
    body: s.InvokeIn,
    workspace: Annotated[str | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
) -> s.InvokeOut:
    return svc.invoke(model_id, body, workspace, project)


# ---------------------------------------------------------------- allowlists
@router.get("/orgs/{org}/model-allowlists", tags=["models"])
def get_allowlist(
    svc: Models, scope_type: Annotated[ScopeType, Query()], scope_id: Annotated[UUID, Query()]
) -> s.AllowlistOut:
    return svc.get_allowlist(scope_type, scope_id)


@router.put("/orgs/{org}/model-allowlists", tags=["models"])
def put_allowlist(
    svc: Models,
    scope_type: Annotated[ScopeType, Query()],
    scope_id: Annotated[UUID, Query()],
    body: s.AllowlistPut,
) -> s.AllowlistOut:
    return svc.put_allowlist(scope_type, scope_id, body)


# ---------------------------------------------------------------- routes
@router.get("/orgs/{org}/model-routes", tags=["models"])
def list_routes(
    svc: Models, limit: LimitQuery = DEFAULT_LIMIT, cursor: CursorQuery = None
) -> Page[s.RouteOut]:
    return svc.list_routes(limit, cursor)


@router.post("/orgs/{org}/model-routes", status_code=201, tags=["models"])
def create_route(svc: Models, body: s.RouteCreate) -> s.RouteOut:
    return svc.create_route(body)


@router.get("/orgs/{org}/model-routes/{route_id}", tags=["models"])
def get_route(svc: Models, route_id: UUID) -> s.RouteOut:
    return svc.get_route(route_id)


@router.patch("/orgs/{org}/model-routes/{route_id}", tags=["models"])
def update_route(svc: Models, route_id: UUID, body: s.RoutePatch) -> s.RouteOut:
    return svc.update_route(route_id, body)


# ---------------------------------------------------------------- the SHI-wide provider
@router.get("/orgs/{org}/shi-provider-access", tags=["models"])
def get_shi_access(svc: Models) -> s.ShiAccessOut:
    return svc.get_shi_access()


@router.get("/platform/shi-provider", tags=["models"])
def get_shi_provider(auth: AuthDep, session: SessionDep) -> s.ProviderOut:
    return service.platform_get_shi_provider(session, auth.principal)


@router.put("/platform/shi-provider", tags=["models"])
def put_shi_provider(
    auth: AuthDep,
    session: SessionDep,
    ctx: RequestCtxDep,
    runtime: RuntimeDep,
    body: s.ShiProviderPut,
) -> s.ProviderOut:
    return service.platform_put_shi_provider(session, ctx, auth.principal, runtime.registry, body)


@router.get("/platform/orgs/{org}/shi-provider-access", tags=["models"])
def get_client_shi_access(auth: AuthDep, session: SessionDep, org: str) -> s.PlatformShiAccessOut:
    return service.platform_get_shi_access(session, auth.principal, org)


@router.put("/platform/orgs/{org}/shi-provider-access", tags=["models"])
def set_client_shi_access(
    auth: AuthDep, session: SessionDep, ctx: RequestCtxDep, org: str, body: s.ShiAccessPut
) -> s.PlatformShiAccessOut:
    return service.platform_set_shi_access(session, ctx, auth.principal, org, body)
