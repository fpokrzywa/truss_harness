"""Adapter registry, vendor catalog and platform vendor routes (contract P3 sections 1-2)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from shifde.adapters.deps import RuntimeDep
from shifde.authz import service as authz
from shifde.authz.service import ResourceRef
from shifde.core import db
from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.errors import Forbidden
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.identity import service as identity
from shifde.identity.dependencies import AuthDep
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep
from shifde.vendors import schemas as s
from shifde.vendors import service

router = APIRouter(prefix="/api/v1")


def _tenant_service(scope: ScopeDep, runtime: RuntimeDep) -> service.VendorService:
    return service.VendorService.for_tenant(scope, runtime.registry)


def _platform_service(
    auth: AuthDep, session: SessionDep, ctx: RequestCtxDep, runtime: RuntimeDep
) -> service.VendorService:
    return service.VendorService.for_platform(session, ctx, auth.principal, runtime.registry)


TenantVendors = Annotated[service.VendorService, Depends(_tenant_service)]
PlatformVendors = Annotated[service.VendorService, Depends(_platform_service)]


# ---------------------------------------------------------------- adapters (global registry)
@router.get("/adapters", tags=["adapters"])
def list_adapters(_auth: AuthDep, session: SessionDep, runtime: RuntimeDep) -> Page[s.AdapterOut]:
    return service.list_adapters(session, runtime.registry)


@router.get("/adapters/{key}/conformance-runs", tags=["adapters"])
def conformance_runs(
    auth: AuthDep, session: SessionDep, runtime: RuntimeDep, key: str
) -> Page[s.ConformanceRunOut]:
    """Evidence history. Global data: allowed for a principal holding `vendor:read` in any of its
    organizations (or a platform admin)."""
    principal = auth.principal
    allowed = principal.is_platform_admin and principal.credential != "api_key"
    if not allowed:
        for m in identity.my_memberships(session):
            if principal.tenant_id not in (None, m.tenant_id):
                continue
            db.set_tenant(session, m.tenant_id)
            pdp = authz.new_pdp(session, principal, m.tenant_id)
            if pdp.authorize("vendor:read", ResourceRef(m.tenant_id)).allowed:
                allowed = True
                break
        db.set_tenant(session, None)
    if not allowed:
        raise Forbidden("missing permission: vendor:read")
    return service.list_conformance_runs(session, runtime.registry, key)


# ---------------------------------------------------------------- tenant vendors
@router.get("/orgs/{org}/vendors", tags=["vendors"])
def list_vendors(
    svc: TenantVendors,
    category: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.VendorOut]:
    return svc.list(category=category, status=status, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/vendors", status_code=201, tags=["vendors"])
def create_vendor(svc: TenantVendors, body: s.VendorCreate) -> s.VendorOut:
    return svc.create(body)


@router.get("/orgs/{org}/vendors/{vendor_id}", tags=["vendors"])
def get_vendor(svc: TenantVendors, vendor_id: UUID) -> s.VendorOut:
    return svc.get(vendor_id)


@router.patch("/orgs/{org}/vendors/{vendor_id}", tags=["vendors"])
def update_vendor(svc: TenantVendors, vendor_id: UUID, body: s.VendorPatch) -> s.VendorOut:
    return svc.update(vendor_id, body)


@router.post("/orgs/{org}/vendors/{vendor_id}/status", tags=["vendors"])
def set_vendor_status(
    svc: TenantVendors, vendor_id: UUID, body: s.VendorStatusChange
) -> s.VendorOut:
    return svc.set_status(vendor_id, body)


@router.get("/orgs/{org}/vendors/{vendor_id}/capabilities", tags=["vendors"])
def vendor_capabilities(svc: TenantVendors, vendor_id: UUID) -> Page[s.VendorCapabilityOut]:
    return svc.capabilities(vendor_id)


# ---------------------------------------------------------------- platform (global catalog)
@router.get("/platform/vendors", tags=["vendors"])
def list_platform_vendors(
    svc: PlatformVendors,
    category: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.VendorOut]:
    return svc.list(category=category, status=status, limit=limit, cursor=cursor)


@router.post("/platform/vendors", status_code=201, tags=["vendors"])
def create_platform_vendor(svc: PlatformVendors, body: s.VendorCreate) -> s.VendorOut:
    return svc.create(body)


@router.get("/platform/vendors/{vendor_id}", tags=["vendors"])
def get_platform_vendor(svc: PlatformVendors, vendor_id: UUID) -> s.VendorOut:
    return svc.get(vendor_id)


@router.patch("/platform/vendors/{vendor_id}", tags=["vendors"])
def update_platform_vendor(
    svc: PlatformVendors, vendor_id: UUID, body: s.VendorPatch
) -> s.VendorOut:
    return svc.update(vendor_id, body)
