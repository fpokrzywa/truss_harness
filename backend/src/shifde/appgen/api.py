"""App request routes (SP5 design section 7): created and listed under a project, `{ws}` and
`{proj}` named as in the tenancy routes; one request is reached under the organization."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from shifde.adapters.deps import RuntimeDep
from shifde.appgen import schemas as s
from shifde.appgen.service import AppGenService
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")
REQUESTS = "/orgs/{org}/workspaces/{ws}/projects/{proj}/app-requests"
REQUEST = "/orgs/{org}/app-requests/{request_id}"


def _service(scope: ScopeDep, runtime: RuntimeDep) -> AppGenService:
    return AppGenService(scope, runtime)


Service = Annotated[AppGenService, Depends(_service)]


@router.post(REQUESTS, status_code=202, tags=["appgen"])
def create_app_request(
    svc: Service, ws: str, proj: str, body: s.AppRequestCreate
) -> s.AppRequestOut:
    return svc.create(ws, proj, body)


@router.get(REQUESTS, tags=["appgen"])
def list_app_requests(
    svc: Service,
    ws: str,
    proj: str,
    status: Annotated[s.RequestStatus | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.AppRequestSummary]:
    return svc.list(ws, proj, status=status, limit=limit, cursor=cursor)


@router.get(REQUEST, tags=["appgen"])
def get_app_request(svc: Service, request_id: UUID) -> s.AppRequestOut:
    return svc.get(request_id)


@router.post(REQUEST + "/drafts", status_code=201, tags=["appgen"])
def revise_app_draft(svc: Service, request_id: UUID, body: s.DraftEdit) -> s.DraftOut:
    return svc.revise(request_id, body)


@router.post(REQUEST + "/discard", tags=["appgen"])
def discard_app_request(svc: Service, request_id: UUID) -> s.AppRequestOut:
    return svc.discard(request_id)


@router.post(REQUEST + "/retry", status_code=202, tags=["appgen"])
def retry_app_request(svc: Service, request_id: UUID) -> s.AppRequestOut:
    return svc.retry(request_id)
