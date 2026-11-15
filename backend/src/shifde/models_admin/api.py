"""Model provider and model routes (contract P3 section 3) and the SHI-wide provider's routes.

Ported from SETH 3ba0e06 `models_admin/api.py`, part 2a: `/model-providers*` and `/models`,
`/models/{model_id}`; allowlists, `/models/effective` (registered before `/models/{model_id}`),
invoke and routes follow in part 2b. The `app` port mounts `router` (B-501).

* Every `{org}` route resolves the client through `tenancy.dependencies.resolve_org` (B-1019).
* `GET /orgs/{org}/shi-provider-access`: the client reads its own switch (`model:read`).
* `/platform/shi-provider` and `/platform/orgs/{org}/shi-provider-access`: the platform
  administrator only (design 3 and 5.3, DECISIONS B-1717): configures the shared provider and
  switches it on or off for a client by slug.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from shifde.adapters.deps import RuntimeDep
from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.identity.dependencies import AuthDep
from shifde.models_admin import schemas as s
from shifde.models_admin import service
from shifde.models_admin.service import ModelService
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")


def _service(scope: ScopeDep, runtime: RuntimeDep) -> ModelService:
    return ModelService(scope, runtime)


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


@router.post("/orgs/{org}/model-providers/{provider_id}/health-check", tags=["models"])
def health_check(svc: Models, provider_id: UUID) -> s.HealthOut:
    return svc.health_check(provider_id)


@router.post("/orgs/{org}/model-providers/{provider_id}/discover-models", tags=["models"])
def discover_models(svc: Models, provider_id: UUID) -> Page[s.DiscoveredModelOut]:
    return svc.discover_models(provider_id)


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


@router.get("/orgs/{org}/models/{model_id}", tags=["models"])
def get_model(svc: Models, model_id: UUID) -> s.ModelOut:
    return svc.get_model(model_id)


@router.patch("/orgs/{org}/models/{model_id}", tags=["models"])
def update_model(svc: Models, model_id: UUID, body: s.ModelPatch) -> s.ModelOut:
    return svc.update_model(model_id, body)


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
