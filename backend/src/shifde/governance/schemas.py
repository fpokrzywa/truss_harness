"""DTOs of the Phase 8 governance API (docs/api/PHASE_8_CONTRACT.md + Amendment G1).

Money and rates are returned as JSON numbers (floats) for display; the exact `numeric` values live
in the database and in each event's `rate_snapshot` (strings).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal  # Any: free-form JSON documents
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Strict = ConfigDict(extra="forbid")

# Bounds match the column precision (rates numeric(24,10), amounts numeric(24,8)) with headroom for
# the multiplication by token counts, and keep NaN / Infinity out of numeric columns.
Rate = Annotated[float, Field(ge=0, le=1e8, allow_inf_nan=False)]
BudgetAmount = Annotated[float, Field(ge=0, le=1e15, allow_inf_nan=False)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
CostKind = Literal["provider_reported", "calculated", "estimated_infra", "unmeasured"]
ScopeType = Literal["tenant", "workspace", "project", "agent", "pipeline", "principal"]


class GovScope(BaseModel):
    model_config = Strict
    type: ScopeType
    id: UUID


# ---------------------------------------------------------------------------- rate cards
class RateCardCreate(BaseModel):
    model_config = Strict
    provider_key: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    model_key: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class RateCardOut(BaseModel):
    id: UUID
    provider_key: str
    model_key: str
    name: str
    scope: Literal["global", "tenant"]
    created_at: datetime


class RateCardVersionCreate(BaseModel):
    model_config = Strict
    effective_from: datetime
    effective_to: datetime | None = None
    currency: Currency
    unit: Literal["per_1m_tokens", "per_call", "per_second"]
    input_rate: Rate
    output_rate: Rate
    cached_read_rate: Rate | None = None
    cached_write_rate: Rate | None = None
    other_rates: dict[str, Rate] = Field(default_factory=dict, max_length=50)
    source: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    revision: Annotated[str, StringConstraints(min_length=1, max_length=100)]


class RateCardVersionOut(BaseModel):
    id: UUID
    rate_card_id: UUID
    version_no: int
    effective_from: datetime
    effective_to: datetime | None
    currency: str
    unit: str
    input_rate: float
    output_rate: float
    cached_read_rate: float | None
    cached_write_rate: float | None
    other_rates: dict[str, Any]
    source: str
    revision: str
    created_at: datetime


class ResolvedRateOut(BaseModel):
    card: RateCardOut
    version: RateCardVersionOut
    at: datetime


# ---------------------------------------------------------------------------- usage
class UsageEventOut(BaseModel):
    id: UUID
    event_key: str
    occurred_at: datetime
    event_type: str
    status: str
    run_id: UUID | None
    root_run_id: UUID | None
    step_id: UUID | None
    step_name: str | None
    attempt: int | None
    workspace_id: UUID | None
    project_id: UUID | None
    principal_id: UUID | None
    agent_id: UUID | None
    model_id: UUID | None
    provider_key: str | None
    model_key: str | None
    is_test: bool
    input_tokens: int
    output_tokens: int
    cached_read_tokens: int
    cached_write_tokens: int
    call_count: int
    duration_ms: int | None
    cost_amount: float | None
    currency: str | None
    cost_kind: CostKind
    rate_card_version_id: UUID | None
    rate_snapshot: dict[str, Any] | None
    corrects_event_id: UUID | None


class CostBreakdownOut(BaseModel):
    provider_reported: float
    calculated: float
    estimated_infra: float
    unmeasured_count: int
    total_known: float


class UsageGroupOut(BaseModel):
    key: str
    label: str | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    calls: int
    duration_ms: int
    cost: CostBreakdownOut
    currency: str | None
    rate_card_versions: list[dict[str, Any]] = Field(default_factory=list)


class UsageTotalsOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    calls: int
    duration_ms: int
    cost: CostBreakdownOut
    currency: str | None
    rate_card_versions: list[dict[str, Any]] = Field(default_factory=list)


class ReconciliationOut(BaseModel):
    events_sum: int
    rollup_sum: int
    consistent: bool
    mismatched_rows: int
    unit: str = "tokens (input + output)"
    level: Literal["totals", "full"] = "full"
    detail: list[dict[str, Any]] = Field(default_factory=list)


class UsageSummaryOut(BaseModel):
    groups: list[UsageGroupOut]
    totals: UsageTotalsOut
    reconciliation: ReconciliationOut
    group_by: str
    from_day: str
    to_day: str
    notes: list[str]


class RunUsageOut(BaseModel):
    run_id: UUID
    events: list[UsageEventOut]
    totals: UsageTotalsOut
    final_reconciled_cost: dict[str, Any]


# ---------------------------------------------------------------------------- budgets / quotas
class BudgetCreate(BaseModel):
    model_config = Strict
    name: Annotated[str, StringConstraints(max_length=200)] = ""
    scope: GovScope
    period: Literal["daily", "monthly", "total"]
    amount: BudgetAmount
    currency: Currency | None = None
    metric: Literal["cost", "tokens"]
    enforcement: Literal["hard", "soft"] = "hard"
    on_exceed: Literal["deny", "queue_until_reset", "require_approval"] = "deny"
    alert_thresholds: list[Annotated[float, Field(gt=0, le=10)]] = Field(
        default_factory=lambda: [0.5, 0.8, 1.0], max_length=10
    )
    status: Literal["active", "disabled"] = "active"


class BudgetPatch(BaseModel):
    """`scope` and `metric` are accepted so a client may echo the whole budget back, but they are
    immutable: a different value is refused (delete / recreate to change what a budget measures)."""

    model_config = Strict
    scope: GovScope | None = None
    metric: Literal["cost", "tokens", "runs"] | None = None  # "runs": echo of a P5 run budget
    name: Annotated[str, StringConstraints(max_length=200)] | None = None
    period: Literal["daily", "monthly", "total"] | None = None
    amount: BudgetAmount | None = None
    currency: Currency | None = None
    enforcement: Literal["hard", "soft"] | None = None
    on_exceed: Literal["deny", "queue_until_reset", "require_approval"] | None = None
    alert_thresholds: list[Annotated[float, Field(gt=0, le=10)]] | None = Field(
        default=None, max_length=10
    )
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class BudgetOut(BaseModel):
    id: UUID
    name: str
    scope: GovScope
    period: str
    amount: float | None
    currency: str | None
    metric: str
    enforcement: str
    on_exceed: str
    on_exceed_effective: str
    alert_thresholds: list[float]
    status: str
    row_version: int
    created_at: datetime


class BudgetStatusOut(BaseModel):
    budget_id: UUID
    period_start: datetime | None
    metric: str
    currency: str | None
    amount: float | None
    reserved: float
    settled: float
    remaining: float | None
    utilization: float
    alerts_fired: list[float]
    stale_holds: int
    on_exceed_effective: str
    note: str | None = None


class QuotaCreate(BaseModel):
    model_config = Strict
    scope: GovScope
    metric: Literal["runs_per_day", "tokens_per_day", "concurrent_runs"]
    limit: Annotated[int, Field(ge=0, le=10**12)]


class QuotaPatch(BaseModel):
    """`scope` and `metric` may be echoed back but cannot change."""

    model_config = Strict
    scope: GovScope | None = None
    metric: Literal["runs_per_day", "tokens_per_day", "concurrent_runs"] | None = None
    limit: Annotated[int, Field(ge=0, le=10**12)] | None = None
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class QuotaOut(BaseModel):
    id: UUID
    scope: GovScope
    metric: str
    limit: int
    status: str
    row_version: int


# ---------------------------------------------------------------------------- policies
PolicyKind = Literal["model_policy", "approval_rule", "data_export", "lifecycle", "cost_threshold"]


class PolicyCreate(BaseModel):
    model_config = Strict
    kind: PolicyKind
    name: Annotated[str, StringConstraints(max_length=200)] = ""
    scope: GovScope | None = None
    definition: dict[str, Any] = Field(default_factory=dict)
    status: Literal["active", "disabled"] = "active"


class PolicyPatch(BaseModel):
    """`kind` and `scope` may be echoed back but cannot change."""

    model_config = Strict
    kind: PolicyKind | None = None
    scope: GovScope | None = None
    name: Annotated[str, StringConstraints(max_length=200)] | None = None
    definition: dict[str, Any] | None = None
    status: Literal["active", "disabled"] | None = None
    row_version: int | None = None


class PolicyOut(BaseModel):
    id: UUID
    kind: str
    name: str
    scope: GovScope | None
    definition: dict[str, Any]
    status: str
    version: int
    row_version: int
    enforcement: str
    created_at: datetime


class DecisionOut(BaseModel):
    id: UUID
    at: datetime
    occurred_at: datetime  # alias of `at` (the P8 UI reads this name)
    decision: Literal["allow", "deny"]
    action: str
    resource: str | None
    reason_codes: list[str]
    reason_code: str | None  # first reason code (the P8 UI reads this name)
    policy_ids: list[UUID]
    run_id: UUID | None
    principal: str | None


class RetentionItem(BaseModel):
    model_config = Strict
    data_class: Literal[
        "run_artifacts", "run_steps_content", "audit_events", "usage_events", "policy_decisions"
    ]
    retain_days: Annotated[int, Field(ge=1, le=36500)]
    legal_hold: bool = False


class RetentionPut(BaseModel):
    model_config = Strict
    items: list[RetentionItem] = Field(min_length=1, max_length=10)


class RetentionItemOut(BaseModel):
    data_class: str
    retain_days: int
    legal_hold: bool
    floor_days: int
    default_days: int
    is_default: bool


class RetentionOut(BaseModel):
    items: list[RetentionItemOut]
    enforcement: Literal["not_implemented"] = "not_implemented"
    note: str


class RetentionPreviewItem(BaseModel):
    data_class: str
    count: int
    cutoff: datetime
    on_legal_hold: bool


class RetentionPreviewOut(BaseModel):
    items: list[RetentionPreviewItem]
    enforcement: Literal["not_implemented"] = "not_implemented"
    note: str


class OverviewBudget(BaseModel):
    id: UUID
    name: str
    scope: GovScope
    metric: str
    currency: str | None
    utilization: float
    enforcement: str
    alert_thresholds: list[float]


class OverviewSpender(BaseModel):
    key: str
    label: str | None
    cost_total_known: float
    cost: CostBreakdownOut
    currency: str | None
    tokens: int


class OverviewOut(BaseModel):
    budgets: list[OverviewBudget]
    top_spenders: list[OverviewSpender]
    denials_7d: int
    pending_approvals: int
    policy_counts: dict[str, int]


# ---------------------------------------------------------------------------- observability
class PerfGroupOut(BaseModel):
    key: str
    label: str | None
    runs: int
    count: int
    error_count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    error_rate: float


class PerformanceOut(BaseModel):
    groups: list[PerfGroupOut]
    group_by: str
    note: str


class HealthDetailsOut(BaseModel):
    database: dict[str, Any]
    migrations: dict[str, Any]
    workers: dict[str, Any]
    queue: dict[str, Any]
    reaper: dict[str, Any]
    recent_error_rates: dict[str, Any]
    telemetry: dict[str, Any]
