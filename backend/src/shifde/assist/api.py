"""Assist routes (SP7 design section 11): the catalog, settings and switch under the organization;
install and the specialists under a project, `{ws}` and `{proj}` named as in the tenancy routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from shifde.adapters.deps import RuntimeDep
from shifde.assist import schemas as s
from shifde.assist.service import AssistService
from shifde.tenancy.dependencies import ScopeDep

router = APIRouter(prefix="/api/v1")
ORG = "/orgs/{org}/assist"
PROJECT = "/orgs/{org}/workspaces/{ws}/projects/{proj}/assist"


def _service(scope: ScopeDep, runtime: RuntimeDep) -> AssistService:
    return AssistService(scope, runtime)


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
