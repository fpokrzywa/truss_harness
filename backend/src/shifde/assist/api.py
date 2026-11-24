"""Assist routes (SP7 design section 11): the catalog, settings and switch under the organization;
install, the specialists and requests under a project, `{ws}` and `{proj}` named as in the tenancy
routes; one request is reached under the organization."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from shifde.adapters.deps import RuntimeDep
from shifde.assist import schemas as s
from shifde.assist.service import AssistService
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")
ORG = "/orgs/{org}/assist"
PROJECT = "/orgs/{org}/workspaces/{ws}/projects/{proj}/assist"


def _service(scope: ScopeDep, runtime: RuntimeDep, request: Request) -> AssistService:
    return AssistService(scope, runtime, request.app.state.governance)


Service = Annotated[AssistService, Depends(_service)]


@router.get(ORG + "/catalog", tags=["assist"])
def get_assist_catalog(svc: Service) -> s.AssistCatalogOut:
    return svc.catalog()


@router.get(ORG + "/settings", tags=["assist"])
def get_assist_settings(svc: Service) -> s.AssistSettingsOut:
    return svc.get_settings()


@router.put(ORG + "/settings", tags=["assist"])
def put_assist_settings(svc: Service, body: s.AssistSettingsPut) -> s.AssistSettingsOut:
    return svc.put_settings(body)


@router.post(ORG + "/enable", tags=["assist"])
def enable_assist(svc: Service) -> s.AssistSettingsOut:
    return svc.enable()


@router.post(ORG + "/disable", tags=["assist"])
def disable_assist(svc: Service) -> s.AssistSettingsOut:
    return svc.disable()


@router.post(PROJECT + "/install", tags=["assist"])
def install_assist_specialists(
    svc: Service, ws: str, proj: str, body: s.InstallIn
) -> list[s.SpecialistOut]:
    return svc.install(ws, proj, body)


@router.get(PROJECT + "/specialists", tags=["assist"])
def list_assist_specialists(svc: Service, ws: str, proj: str) -> list[s.SpecialistOut]:
    return svc.list_specialists(ws, proj)


@router.post(PROJECT + "/specialists/{key}/enable", tags=["assist"])
def enable_assist_specialist(svc: Service, ws: str, proj: str, key: str) -> s.SpecialistOut:
    return svc.enable_specialist(ws, proj, key)


@router.post(PROJECT + "/specialists/{key}/disable", tags=["assist"])
def disable_assist_specialist(svc: Service, ws: str, proj: str, key: str) -> s.SpecialistOut:
    return svc.disable_specialist(ws, proj, key)


@router.post(
    PROJECT + "/requests",
    status_code=201,
    tags=["assist"],
    responses={
        202: {
            "model": s.AssistRequestOut,
            "description": "Queued: the router model classifies the request",
        }
    },
)
def create_assist_request(
    svc: Service, response: Response, ws: str, proj: str, body: s.AssistRequestIn
) -> s.AssistRequestOut:
    out = svc.create_request(ws, proj, body)
    if out.status == "queued":
        response.status_code = 202
    return out


@router.get(PROJECT + "/requests", tags=["assist"])
def list_assist_requests(
    svc: Service,
    ws: str,
    proj: str,
    status: Annotated[s.RequestStatus | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.AssistRequestSummary]:
    return svc.list_requests(ws, proj, status=status, limit=limit, cursor=cursor)


@router.get(ORG + "/requests/{request_id}", tags=["assist"])
def get_assist_request(svc: Service, request_id: UUID) -> s.AssistRequestOut:
    return svc.get_request(request_id)
