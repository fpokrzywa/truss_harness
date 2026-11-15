"""Governance routes. Part 1 (DECISIONS B-1801): the rate-card routes, platform and client.

Part 2 (B-1811) adds usage, budgets, quotas and the audit export; policies, retention, decisions,
overview, performance and platform health come with part 3. The `app` port mounts `router` (B-501).

Every `{org}` route resolves the client through `tenancy.dependencies.resolve_org`
(`ScopeDep`): an organization the caller has no binding in answers like an unknown one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from shifde.core.deps import RequestCtxDep, SessionDep
from shifde.core.pagination import DEFAULT_LIMIT, Page
from shifde.governance import schemas as s
from shifde.governance.service import GovernanceService, PricingService
from shifde.identity.dependencies import AuthDep
from shifde.tenancy.dependencies import CursorQuery, LimitQuery, ScopeDep

router = APIRouter(prefix="/api/v1")

FromQuery = Annotated[datetime | None, Query(alias="from")]
ToQuery = Annotated[datetime | None, Query(alias="to")]


def _tenant_pricing(scope: ScopeDep) -> PricingService:
    return PricingService.for_tenant(scope)


def _platform_pricing(auth: AuthDep, session: SessionDep, ctx: RequestCtxDep) -> PricingService:
    return PricingService.for_platform(session, ctx, auth.principal)


def _gov(scope: ScopeDep) -> GovernanceService:
    return GovernanceService(scope)


TenantPricing = Annotated[PricingService, Depends(_tenant_pricing)]
PlatformPricing = Annotated[PricingService, Depends(_platform_pricing)]
Gov = Annotated[GovernanceService, Depends(_gov)]


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


# ---------------------------------------------------------------- usage
@router.get("/orgs/{org}/usage/events", tags=["usage"])
def usage_events(
    svc: Gov,
    run_id: UUID | None = None,
    from_: FromQuery = None,
    to: ToQuery = None,
    event_type: Annotated[str | None, Query(max_length=40)] = None,
    limit: LimitQuery = DEFAULT_LIMIT,
    cursor: CursorQuery = None,
) -> Page[s.UsageEventOut]:
    return svc.list_events(
        run_id=run_id, start=from_, end=to, event_type=event_type, limit=limit, cursor=cursor
    )


@router.get("/orgs/{org}/usage/summary", tags=["usage"])
def usage_summary(
    svc: Gov,
    group_by: Annotated[str, Query(max_length=20)] = "project",
    from_: FromQuery = None,
    to: ToQuery = None,
    workspace: Annotated[str | None, Query(max_length=100)] = None,
    project: Annotated[str | None, Query(max_length=100)] = None,
    cost_kind: Annotated[str | None, Query(max_length=30)] = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    reconcile: Annotated[str, Query(pattern=r"^(totals|full)$")] = "totals",
) -> s.UsageSummaryOut:
    """`reconcile=totals` (default) compares control totals per day and cost kind; `full`
    compares every dimension row of the window (slow on very large tenants)."""
    return svc.summary(
        group_by=group_by,
        start=from_,
        end=to,
        workspace=workspace,
        project=project,
        cost_kind=cost_kind,
        currency=currency,
        deep=reconcile == "full",
    )


@router.get("/orgs/{org}/usage/runs/{run_id}", tags=["usage"])
def run_usage(svc: Gov, run_id: UUID) -> s.RunUsageOut:
    return svc.run_usage(run_id)


@router.post("/orgs/{org}/usage/rollups/rebuild", tags=["usage"])
def rebuild_rollups(svc: Gov, from_: FromQuery = None, to: ToQuery = None) -> s.ReconciliationOut:
    return svc.rebuild_rollups(from_, to)


@router.get("/orgs/{org}/usage/export", tags=["usage"])
def export_usage(
    svc: Gov,
    format: Annotated[str, Query(max_length=10)] = "csv",
    from_: FromQuery = None,
    to: ToQuery = None,
    workspace: Annotated[str | None, Query(max_length=100)] = None,
    project: Annotated[str | None, Query(max_length=100)] = None,
    cost_kind: Annotated[str | None, Query(max_length=30)] = None,
) -> Response:
    body, truncated = svc.export_usage(
        fmt=format, start=from_, end=to, workspace=workspace, project=project, cost_kind=cost_kind
    )
    headers = {"Content-Disposition": 'attachment; filename="usage.csv"'}
    if truncated:
        headers["X-Export-Truncated"] = "true"
    return Response(body, media_type="text/csv", headers=headers)


# ---------------------------------------------------------------- budgets / quotas
@router.get("/orgs/{org}/budgets", tags=["budgets"])
def list_budgets(svc: Gov) -> Page[s.BudgetOut]:
    return svc.list_budgets()


@router.post("/orgs/{org}/budgets", status_code=201, tags=["budgets"])
def create_budget(svc: Gov, body: s.BudgetCreate) -> s.BudgetOut:
    return svc.create_budget(body)


@router.get("/orgs/{org}/budgets/{budget_id}", tags=["budgets"])
def get_budget(svc: Gov, budget_id: UUID) -> s.BudgetOut:
    return svc.get_budget(budget_id)


@router.patch("/orgs/{org}/budgets/{budget_id}", tags=["budgets"])
def update_budget(svc: Gov, budget_id: UUID, body: s.BudgetPatch) -> s.BudgetOut:
    return svc.update_budget(budget_id, body)


@router.get("/orgs/{org}/budgets/{budget_id}/status", tags=["budgets"])
def budget_status(svc: Gov, budget_id: UUID) -> s.BudgetStatusOut:
    return svc.budget_status(budget_id)


@router.get("/orgs/{org}/quotas", tags=["budgets"])
def list_quotas(svc: Gov) -> Page[s.QuotaOut]:
    return svc.list_quotas()


@router.post("/orgs/{org}/quotas", status_code=201, tags=["budgets"])
def create_quota(svc: Gov, body: s.QuotaCreate) -> s.QuotaOut:
    return svc.create_quota(body)


@router.get("/orgs/{org}/quotas/{quota_id}", tags=["budgets"])
def get_quota(svc: Gov, quota_id: UUID) -> s.QuotaOut:
    return svc.get_quota(quota_id)


@router.patch("/orgs/{org}/quotas/{quota_id}", tags=["budgets"])
def update_quota(svc: Gov, quota_id: UUID, body: s.QuotaPatch) -> s.QuotaOut:
    return svc.update_quota(quota_id, body)


# ---------------------------------------------------------------- audit export
@router.get("/orgs/{org}/audit-events/export", tags=["audit"])
def export_audit(
    svc: Gov,
    format: Annotated[str, Query(max_length=10)] = "csv",
    category: Annotated[str | None, Query(max_length=30)] = None,
    from_: FromQuery = None,
    to: ToQuery = None,
) -> Response:
    body, media, truncated = svc.export_audit(fmt=format, category=category, start=from_, end=to)
    ext = "csv" if media == "text/csv" else "ndjson"
    headers = {"Content-Disposition": f'attachment; filename="audit.{ext}"'}
    if truncated:
        headers["X-Export-Truncated"] = "true"
    return Response(body, media_type=media, headers=headers)
