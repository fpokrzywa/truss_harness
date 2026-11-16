// Phase 8 shapes (docs/api/PHASE_8_CONTRACT.md). Where the contract is silent the types are tolerant;
// see frontend/README.md Decisions (Phase 8).

export type CostKind = 'provider_reported' | 'calculated' | 'estimated_infra' | 'unmeasured';
export const COST_KINDS: readonly CostKind[] = [
  'provider_reported',
  'calculated',
  'estimated_infra',
  'unmeasured',
];

export const GROUP_BY = [
  'workspace',
  'project',
  'user',
  'agent',
  'prompt',
  'model',
  'pipeline',
  'workflow',
  'run',
  'build',
] as const;
export type GroupBy = (typeof GROUP_BY)[number];

/** Per-kind subtotals. Unmeasured events have no amount, only a count. */
export interface CostBreakdown {
  provider_reported: number;
  calculated: number;
  estimated_infra: number;
  unmeasured_count: number;
  total_known: number;
}

export interface UsageGroup {
  key: string;
  label?: string | null;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  calls: number;
  duration_ms: number;
  cost: CostBreakdown;
  currency?: string | null;
  /** Optional: rate card version numbers the calculated amounts used (server sends {id, version_no}). */
  rate_card_versions?: (string | number)[];
}

export interface UsageTotals {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  calls: number;
  duration_ms: number;
  cost: CostBreakdown;
  currency?: string | null;
  rate_card_versions?: (string | number)[];
}

export interface Reconciliation {
  /** Total tokens (input + output) summed from the ledger events. */
  events_sum: number;
  /** Total tokens (input + output) summed from the rollups. */
  rollup_sum: number;
  consistent: boolean;
  /** Rollup rows that disagree with the events (full reconciliation only). */
  mismatched_rows?: number;
  /** `totals`: only the grand totals were compared; `full`: every rollup row was compared. */
  level?: 'totals' | 'full';
}

export interface UsageSummary {
  groups: UsageGroup[];
  totals: UsageTotals;
  reconciliation: Reconciliation;
}

export interface UsageFilters {
  group_by?: GroupBy;
  from?: string;
  to?: string;
  workspace?: string;
  project?: string;
  cost_kind?: CostKind | '';
}

export interface UsageEvent {
  id: string;
  event_key?: string;
  occurred_at: string;
  event_type: string;
  run_id?: string | null;
  step_id?: string | null;
  step_name?: string | null;
  attempt?: number | null;
  model_id?: string | null;
  provider_key?: string | null;
  input_tokens?: number;
  output_tokens?: number;
  cached_read_tokens?: number;
  cached_write_tokens?: number;
  duration_ms?: number | null;
  cost_amount?: number | null;
  currency?: string | null;
  cost_kind: CostKind;
  rate_card_version_id?: string | null;
  rate_snapshot?: Record<string, unknown> | null;
  corrects_event_id?: string | null;
}

export interface EventFilters {
  run_id?: string;
  from?: string;
  to?: string;
  event_type?: string;
  cursor?: string;
}

// ---- Pricing ------------------------------------------------------------------------------------

export interface RateCard {
  id: string;
  provider_key: string;
  model_key: string;
  name: string;
  scope?: 'global' | 'tenant';
}

export type RateUnit = 'per_1m_tokens' | 'per_call' | 'per_second';

export interface RateCardVersion {
  id: string;
  rate_card_id?: string;
  version_no?: number;
  effective_from: string;
  effective_to?: string | null;
  currency: string;
  unit: RateUnit;
  input_rate: number;
  output_rate: number;
  cached_read_rate?: number | null;
  cached_write_rate?: number | null;
  other_rates?: Record<string, number> | null;
  source: string;
  revision: string | number;
  created_at?: string;
}

export interface RateCardInput {
  provider_key: string;
  model_key: string;
  name: string;
}

export interface RateCardVersionInput {
  effective_from: string;
  effective_to?: string;
  currency: string;
  unit: RateUnit;
  input_rate: number;
  output_rate: number;
  cached_read_rate?: number;
  cached_write_rate?: number;
  other_rates?: Record<string, number>;
  source: string;
  revision: string;
}

// ---- Budgets & quotas ---------------------------------------------------------------------------

export type BudgetScopeType =
  'tenant' | 'workspace' | 'project' | 'agent' | 'pipeline' | 'principal';
export const BUDGET_SCOPE_TYPES: readonly BudgetScopeType[] = [
  'tenant',
  'workspace',
  'project',
  'agent',
  'pipeline',
  'principal',
];

export interface GovScope {
  type: BudgetScopeType;
  id: string;
}

export type BudgetPeriod = 'daily' | 'monthly' | 'total';
export type BudgetMetric = 'cost' | 'tokens';
/** Metric as reported by the API: legacy P5 run-count budgets are reported as `runs`. */
export type BudgetReportedMetric = BudgetMetric | 'runs';
export type Enforcement = 'hard' | 'soft';
export type OnExceed = 'deny' | 'queue_until_reset' | 'require_approval';

export interface BudgetInput {
  name?: string;
  scope: GovScope;
  period: BudgetPeriod;
  amount: number;
  /** Required for cost budgets; omitted for token budgets. */
  currency?: string;
  metric: BudgetMetric;
  enforcement: Enforcement;
  on_exceed: OnExceed;
  alert_thresholds: number[];
  status?: string;
}

export interface Budget extends Omit<BudgetInput, 'amount' | 'currency' | 'metric'> {
  id: string;
  metric: BudgetReportedMetric;
  status: string;
  row_version?: number;
  amount: number | null;
  currency?: string | null;
  /** What the platform actually does when the budget is exceeded (may differ from `on_exceed`):
   * `deny` for hard budgets, `alert_only` for soft budgets. */
  on_exceed_effective?: string;
}

export interface BudgetStatus {
  period_start: string | null;
  reserved: number;
  settled: number;
  remaining: number | null;
  /** Fraction of the amount used (0.8 = 80%). */
  utilization: number;
  /** Threshold fractions that have fired (0.5 = 50%). */
  alerts_fired: (number | string)[];
  on_exceed_effective?: string;
  stale_holds?: number;
  note?: string | null;
}

export type QuotaMetric = 'runs_per_day' | 'tokens_per_day' | 'concurrent_runs';

export interface QuotaInput {
  scope: GovScope;
  metric: QuotaMetric;
  limit: number;
}

export interface Quota extends QuotaInput {
  id: string;
  row_version?: number;
}

// ---- Governance ---------------------------------------------------------------------------------

export type PolicyKind =
  'model_policy' | 'approval_rule' | 'data_export' | 'lifecycle' | 'cost_threshold';
export const POLICY_KINDS: readonly PolicyKind[] = [
  'model_policy',
  'approval_rule',
  'data_export',
  'lifecycle',
  'cost_threshold',
];

export interface GovernancePolicy {
  id: string;
  kind: PolicyKind;
  name?: string | null;
  scope?: GovScope | null;
  definition: Record<string, unknown>;
  status: string;
  version?: number;
  row_version?: number;
  /** `enforced` or `not_enforced` (lifecycle policies are stored and reported only). */
  enforcement?: string;
}

export interface GovernancePolicyInput {
  kind: PolicyKind;
  name?: string;
  scope?: GovScope;
  definition: Record<string, unknown>;
  status: string;
}

export interface RetentionRow {
  data_class: string;
  retain_days: number;
  legal_hold: boolean;
  /** Platform floor in days, when the server tells us. */
  floor_days?: number | null;
  default_days?: number;
  is_default?: boolean;
}

export interface RetentionPreviewRow {
  data_class: string;
  count: number;
  cutoff?: string;
  on_legal_hold?: boolean;
}

/** Retention responses always say whether deletion is actually performed. */
export interface RetentionSettings {
  items: RetentionRow[];
  enforcement: string;
  note: string | null;
}

export interface RetentionPreview {
  items: RetentionPreviewRow[];
  enforcement: string;
  note: string | null;
}

export interface PolicyDecision {
  id: string;
  /** When the decision was made (G1 name). */
  at?: string;
  /** Server alias of `at`. */
  occurred_at?: string;
  run_id?: string | null;
  decision: string;
  action?: string;
  resource?: string | null;
  /** G1 name. */
  reason_codes?: string[];
  /** Server alias: the first reason code. */
  reason_code?: string | null;
  policy_ids?: string[];
  principal?: string | null;
}

export interface DecisionFilters {
  run_id?: string;
  decision?: string;
  action?: string;
  cursor?: string;
}

export interface OverviewBudget {
  id: string;
  label?: string | null;
  scope?: GovScope;
  metric?: BudgetReportedMetric;
  currency?: string;
  utilization: number;
  enforcement?: string;
  alert_thresholds?: number[];
}

export interface OverviewSpender {
  key: string;
  label?: string | null;
  /** Total known cost (G1 name; excludes unmeasured events). */
  cost_total_known: number;
  cost?: CostBreakdown;
  currency?: string | null;
  tokens?: number;
}

export interface GovernanceOverview {
  budgets: OverviewBudget[];
  top_spenders: OverviewSpender[];
  denials_7d: number;
  pending_approvals: number;
  policy_counts: Record<string, number>;
}

// ---- Observability ------------------------------------------------------------------------------

export type PerfGroupBy = 'agent' | 'model' | 'pipeline';

export interface PerfRow {
  key: string;
  label?: string | null;
  /** Runs (or model call attempts when grouped by model). */
  count: number;
  error_count?: number;
  error_rate: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
}

// ---- Platform health details (platform admins) ---------------------------------------------------

export interface HealthDetails {
  queue: { queued: number; leased: number; dead_in_window: number };
  workers: { running: number; known: number };
  telemetry: {
    /** `otlp`, `console` or `none`: what the tracer actually does. */
    exporter: string;
    service?: string;
    otlp: { endpoint: string; exported: number; failed: number } | null;
    buffered_spans: number;
    metrics?: string;
  };
}
