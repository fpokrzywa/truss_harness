"""Governance routes. Part 1 (DECISIONS B-1801): the rate-card routes, platform and client.

Usage, budgets, quotas, policies, retention, decisions, audit export, performance and platform
health come with parts 2 and 3. The `app` port mounts `router` (B-501).

Every `{org}` route resolves the client through `tenancy.dependencies.resolve_org`
(`ScopeDep`): an organization the caller has no binding in answers like an unknown one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.pagination import Page
from shifde.governance import schemas as s
from shifde.governance.service import PricingService
from shifde.identity.dependencies import AuthDep
from shifde.tenancy.dependencies import ScopeDep

router = APIRouter(prefix="/api/v1")


def _tenant_pricing(scope: ScopeDep) -> PricingService:
    return PricingService.for_tenant(scope)


def _platform_pricing(auth: AuthDep, session: SessionDep, ctx: RequestCtxDep) -> PricingService:
    return PricingService.for_platform(session, ctx, auth.principal)


TenantPricing = Annotated[PricingService, Depends(_tenant_pricing)]
PlatformPricing = Annotated[PricingService, Depends(_platform_pricing)]


# ---------------------------------------------------------------- rate cards (platform)
@router.get("/platform/rate-cards", tags=["pricing"])
def list_platform_cards(svc: PlatformPricing) -> Page[s.RateCardOut]:
    return svc.list_cards()


@router.post("/platform/rate-cards", status_code=201, tags=["pricing"])
def create_platform_card(svc: PlatformPricing, body: s.RateCardCreate) -> s.RateCardOut:
    return svc.create_card(body)


@router.get("/platform/rate-cards/{card_id}/versions", tags=["pricing"])
def platform_versions(svc: PlatformPricing, card_id: UUID) -> Page[s.RateCardVersionOut]:
    return svc.list_versions(card_id)


@router.post("/platform/rate-cards/{card_id}/versions", status_code=201, tags=["pricing"])
def create_platform_version(
    svc: PlatformPricing, card_id: UUID, body: s.RateCardVersionCreate
) -> s.RateCardVersionOut:
    return svc.create_version(card_id, body)


# ---------------------------------------------------------------- rate cards (client)
@router.get("/orgs/{org}/rate-cards", tags=["pricing"])
def list_cards(svc: TenantPricing) -> Page[s.RateCardOut]:
    return svc.list_cards()


@router.post("/orgs/{org}/rate-cards", status_code=201, tags=["pricing"])
def create_card(svc: TenantPricing, body: s.RateCardCreate) -> s.RateCardOut:
    return svc.create_card(body)


@router.get("/orgs/{org}/rate-cards/resolve", tags=["pricing"])
def resolve_rate(
    svc: TenantPricing,
    provider_key: Annotated[str, Query(min_length=1, max_length=100)],
    model_key: Annotated[str, Query(min_length=1, max_length=300)],
    at: datetime | None = None,
) -> s.ResolvedRateOut:
    return svc.resolve(provider_key, model_key, at)


@router.get("/orgs/{org}/rate-cards/{card_id}/versions", tags=["pricing"])
def card_versions(svc: TenantPricing, card_id: UUID) -> Page[s.RateCardVersionOut]:
    return svc.list_versions(card_id)


@router.post("/orgs/{org}/rate-cards/{card_id}/versions", status_code=201, tags=["pricing"])
def create_version(
    svc: TenantPricing, card_id: UUID, body: s.RateCardVersionCreate
) -> s.RateCardVersionOut:
    return svc.create_version(card_id, body)
