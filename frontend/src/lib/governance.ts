import type {
  CostBreakdown,
  CostKind,
  GovernanceOverview,
  OverviewBudget,
  OverviewSpender,
  PerfRow,
  Reconciliation,
  RetentionPreview,
  RetentionSettings,
  UsageEvent,
  UsageGroup,
  UsageSummary,
  UsageTotals,
} from './api/governance-types';

const isRec = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const num = (v: unknown): number => {
  const n = typeof v === 'string' ? Number(v) : v;
  return typeof n === 'number' && Number.isFinite(n) ? n : 0;
};
const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null);

// ---- Honesty labels (contract section 0) ----------------------------------------------------------

const versionText = (versions: readonly (string | number)[] | undefined): string => {
  if (!versions || versions.length === 0) return 'rate card';
  // Several cards can each be on "v1", so more than one version is reported as a count.
  if (versions.length > 1) return `${versions.length} rate card versions`;
  const v = String(versions[0]);
  return `rate card ${/^\d+$/.test(v) ? `v${v}` : v}`;
};

/** The one place cost-kind wording lives. Every money figure must render one of these next to it. */
export function costKindLabel(
  kind: CostKind,
  opts: { versions?: readonly (string | number)[]; count?: number } = {},
): string {
  switch (kind) {
    case 'calculated':
      return `Calculated from ${versionText(opts.versions)} (not an invoice)`;
    case 'provider_reported':
      return 'Provider-reported';
    case 'estimated_infra':
      return 'Estimated infrastructure';
    case 'unmeasured': {
      const n = opts.count ?? 0;
      return `Unmeasured (${n} event${n === 1 ? '' : 's'})`;
    }
  }
}

export const TOTAL_KNOWN_LABEL = 'Total known';
export const TOTAL_KNOWN_NOTE =
  'Sum of provider-reported, calculated and estimated amounts. Excludes unmeasured events. Not an invoice.';

/** Money with its currency; sub-cent amounts keep up to six decimals so small costs are not shown as 0. */
export function formatMoney(amount: number, currency?: string | null): string {
  const digits = { minimumFractionDigits: 2, maximumFractionDigits: 6 };
  if (currency && /^[A-Za-z]{3}$/.test(currency)) {
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency: currency.toUpperCase(),
        ...digits,
      }).format(amount);
    } catch {
      // Unknown ISO code: fall through to the plain form.
    }
  }
  const text = new Intl.NumberFormat(undefined, digits).format(amount);
  return currency ? `${text} ${currency}` : text;
}

export const formatCount = (n: number): string => new Intl.NumberFormat().format(n);

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

export const formatPercent = (fraction: number, digits = 0): string =>
  `${(fraction * 100).toFixed(digits)}%`;

// ---- Normalizers ----------------------------------------------------------------------------------

export const emptyCost = (): CostBreakdown => ({
  provider_reported: 0,
  calculated: 0,
  estimated_infra: 0,
  unmeasured_count: 0,
  total_known: 0,
});

export function normalizeCost(raw: unknown): CostBreakdown {
  if (!isRec(raw)) return emptyCost();
  const provider_reported = num(raw.provider_reported);
  const calculated = num(raw.calculated);
  const estimated_infra = num(raw.estimated_infra);
  return {
    provider_reported,
    calculated,
    estimated_infra,
    unmeasured_count: num(raw.unmeasured_count),
    total_known:
      raw.total_known === undefined
        ? provider_reported + calculated + estimated_infra
        : num(raw.total_known),
  };
}

/** Rate card versions arrive as `{id, version_no}`; bare numbers/strings are tolerated. */
const versions = (v: unknown): (string | number)[] | undefined => {
  if (!Array.isArray(v)) return undefined;
  const out: (string | number)[] = [];
  for (const x of v) {
    if (typeof x === 'string' || typeof x === 'number') out.push(x);
    else if (isRec(x) && (typeof x.version_no === 'number' || typeof x.version_no === 'string'))
      out.push(x.version_no);
  }
  return out;
};

function normalizeTotals(raw: unknown): UsageTotals {
  const r = isRec(raw) ? raw : {};
  return {
    input_tokens: num(r.input_tokens),
    output_tokens: num(r.output_tokens),
    cached_tokens: num(r.cached_tokens),
    calls: num(r.calls),
    duration_ms: num(r.duration_ms),
    cost: normalizeCost(r.cost),
    currency: str(r.currency),
    rate_card_versions: versions(r.rate_card_versions),
  };
}

export function normalizeSummary(raw: unknown): UsageSummary {
  const r = isRec(raw) ? raw : {};
  const groups: UsageGroup[] = (Array.isArray(r.groups) ? r.groups : [])
    .filter(isRec)
    .map((g) => ({ ...normalizeTotals(g), key: str(g.key) ?? '', label: str(g.label) }));
  const rec = isRec(r.reconciliation) ? r.reconciliation : {};
  const reconciliation: Reconciliation = {
    events_sum: num(rec.events_sum),
    rollup_sum: num(rec.rollup_sum),
    // Anything but an explicit true is shown as not consistent: never imply a check that did not happen.
    consistent: rec.consistent === true,
    ...(rec.mismatched_rows !== undefined ? { mismatched_rows: num(rec.mismatched_rows) } : {}),
    ...(rec.level === 'totals' || rec.level === 'full' ? { level: rec.level } : {}),
  };
  const totals = normalizeTotals(r.totals);
  if (!isRec(r.totals) && groups.length > 0) {
    // No totals from the server: derive them (still labelled per kind).
    const cost = emptyCost();
    for (const g of groups) {
      totals.input_tokens += g.input_tokens;
      totals.output_tokens += g.output_tokens;
      totals.cached_tokens += g.cached_tokens;
      totals.calls += g.calls;
      totals.duration_ms += g.duration_ms;
      cost.provider_reported += g.cost.provider_reported;
      cost.calculated += g.cost.calculated;
      cost.estimated_infra += g.cost.estimated_infra;
      cost.unmeasured_count += g.cost.unmeasured_count;
      cost.total_known += g.cost.total_known;
    }
    totals.cost = cost;
    totals.currency = groups[0]?.currency ?? null;
  }
  return { groups, totals, reconciliation };
}

export function normalizeOverview(raw: unknown): GovernanceOverview {
  const r = isRec(raw) ? raw : {};
  const budgets: OverviewBudget[] = (Array.isArray(r.budgets) ? r.budgets : [])
    .filter(isRec)
    .map((b) => ({
      id: str(b.id) ?? '',
      label: str(b.label) ?? str(b.name),
      ...(str(b.enforcement) ? { enforcement: str(b.enforcement) as string } : {}),
      ...(isRec(b.scope) ? { scope: b.scope as unknown as OverviewBudget['scope'] } : {}),
      ...(b.metric === 'cost' || b.metric === 'tokens' || b.metric === 'runs'
        ? { metric: b.metric }
        : {}),
      ...(str(b.currency) ? { currency: str(b.currency) as string } : {}),
      utilization: num(b.utilization),
      alert_thresholds: Array.isArray(b.alert_thresholds) ? b.alert_thresholds.map(num) : undefined,
    }));
  const top_spenders: OverviewSpender[] = (Array.isArray(r.top_spenders) ? r.top_spenders : [])
    .filter(isRec)
    .map((s) => {
      const cost = isRec(s.cost) ? normalizeCost(s.cost) : undefined;
      return {
        key: str(s.key) ?? '',
        label: str(s.label),
        // G1 name first; the server also sends the full `cost` breakdown.
        cost_total_known:
          s.cost_total_known !== undefined ? num(s.cost_total_known) : (cost?.total_known ?? 0),
        cost:
          cost && s.cost_total_known !== undefined
            ? { ...cost, total_known: num(s.cost_total_known) }
            : cost,
        currency: str(s.currency),
        tokens: s.tokens === undefined ? undefined : num(s.tokens),
      };
    });
  const counts: Record<string, number> = {};
  if (isRec(r.policy_counts))
    for (const [k, v] of Object.entries(r.policy_counts)) counts[k] = num(v);
  return {
    budgets,
    top_spenders,
    denials_7d: num(r.denials_7d),
    pending_approvals: num(r.pending_approvals),
    policy_counts: counts,
  };
}

export function normalizePerf(raw: unknown): PerfRow[] {
  const rows = Array.isArray(raw)
    ? raw
    : isRec(raw)
      ? ((raw.items ?? raw.rows ?? raw.groups) as unknown)
      : [];
  return (Array.isArray(rows) ? rows : []).filter(isRec).map((r) => ({
    key: str(r.key) ?? '',
    label: str(r.label),
    count: num(r.count ?? r.runs ?? r.calls),
    ...(r.error_count !== undefined ? { error_count: num(r.error_count) } : {}),
    error_rate: num(r.error_rate),
    p50_ms: num(r.p50_ms ?? r.p50),
    p95_ms: num(r.p95_ms ?? r.p95),
    p99_ms: num(r.p99_ms ?? r.p99),
  }));
}

const retentionRow = (r: Record<string, unknown>): RetentionSettings['items'][number] => ({
  data_class: str(r.data_class) ?? '',
  retain_days: num(r.retain_days),
  legal_hold: r.legal_hold === true,
  floor_days: typeof r.floor_days === 'number' ? r.floor_days : null,
  ...(typeof r.default_days === 'number' ? { default_days: r.default_days } : {}),
  ...(typeof r.is_default === 'boolean' ? { is_default: r.is_default } : {}),
});

/**
 * Retention responses carry `enforcement`. Anything other than an explicit enforced value is treated as
 * "not implemented", so the UI never implies that data is deleted.
 */
export function normalizeRetention(raw: unknown): RetentionSettings {
  const r = isRec(raw) ? raw : {};
  const items = Array.isArray(raw) ? raw : Array.isArray(r.items) ? r.items : [];
  return {
    items: items.filter(isRec).map(retentionRow),
    enforcement: str(r.enforcement) ?? 'not_implemented',
    note: str(r.note),
  };
}

export function normalizeRetentionPreview(raw: unknown): RetentionPreview {
  const r = isRec(raw) ? raw : {};
  const items = Array.isArray(raw) ? raw : Array.isArray(r.items) ? r.items : [];
  return {
    items: items.filter(isRec).map((x) => ({
      data_class: str(x.data_class) ?? '',
      count: num(x.count),
      ...(str(x.cutoff) ? { cutoff: str(x.cutoff) as string } : {}),
      ...(typeof x.on_legal_hold === 'boolean' ? { on_legal_hold: x.on_legal_hold } : {}),
    })),
    enforcement: str(r.enforcement) ?? 'not_implemented',
    note: str(r.note),
  };
}

export const RETENTION_NOT_ENFORCED_TEXT =
  'Retention is not enforced yet: these settings are stored and audited, but no data is deleted.';

// ---- Time series from the usage ledger -----------------------------------------------------------

export interface DayPoint {
  day: string;
  input_tokens: number;
  output_tokens: number;
  events: number;
}

/** Sums ledger events per UTC day (corrections are compensating rows and are summed like any other). */
export function dailyTokens(events: readonly UsageEvent[]): DayPoint[] {
  const by = new Map<string, DayPoint>();
  for (const e of events) {
    const day = e.occurred_at.slice(0, 10);
    const p = by.get(day) ?? { day, input_tokens: 0, output_tokens: 0, events: 0 };
    p.input_tokens += e.input_tokens ?? 0;
    p.output_tokens += e.output_tokens ?? 0;
    p.events += 1;
    by.set(day, p);
  }
  return [...by.values()].sort((a, b) => a.day.localeCompare(b.day));
}

/** Sums of a run's events by cost kind (corrections included as compensating rows). */
export function eventCostTotals(events: readonly UsageEvent[]): CostBreakdown {
  const c = emptyCost();
  for (const e of events) {
    if (e.cost_kind === 'unmeasured') c.unmeasured_count += 1;
    else if (typeof e.cost_amount === 'number') c[e.cost_kind] += e.cost_amount;
  }
  c.total_known = c.provider_reported + c.calculated + c.estimated_infra;
  return c;
}

/** Version label for an event's rate snapshot, e.g. `3` from `{version: 3}` (falls back to the id). */
export function snapshotVersion(e: UsageEvent): string | null {
  const s = e.rate_snapshot;
  if (s) {
    const v = s.version_no ?? s.version ?? s.revision;
    if (typeof v === 'number' || (typeof v === 'string' && v !== '')) return String(v);
  }
  return e.rate_card_version_id ?? null;
}

// ---- Chart helpers --------------------------------------------------------------------------------

/** Round tick values from 0 up to at least `max` (1/2/5 steps). */
export function niceTicks(max: number, target = 4): number[] {
  if (!(max > 0)) return [0, 1];
  const rough = max / target;
  const pow = 10 ** Math.floor(Math.log10(rough));
  const f = rough / pow;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
  const out: number[] = [];
  for (let v = 0; v < max + step; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

export const compactNumber = (n: number): string =>
  new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n);

// ---- Budgets ----------------------------------------------------------------------------------------

/** `"50, 80, 100"` (percent) to `[0.5, 0.8, 1]`; null when any entry is not a positive number. */
export function parseThresholds(text: string): number[] | null {
  const parts = text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  const out: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isFinite(n) || n <= 0 || n > 1000) return null;
    out.push(Number((n / 100).toPrecision(10)));
  }
  return [...new Set(out)].sort((a, b) => a - b);
}

export const thresholdsText = (t: readonly number[]): string =>
  t.map((x) => Number((x * 100).toPrecision(10))).join(', ');

/** Whether a threshold has been crossed by the current utilization. */
export const crossed = (utilization: number, threshold: number): boolean =>
  utilization >= threshold;

// ---- Retention ---------------------------------------------------------------------------------------

/** Platform floors the contract names: audit events are kept at least 90 days. Others come from the server. */
export const DEFAULT_FLOORS: Record<string, number> = { audit_events: 90 };

export function floorFor(row: { data_class: string; floor_days?: number | null }): number | null {
  if (typeof row.floor_days === 'number') return row.floor_days;
  return DEFAULT_FLOORS[row.data_class] ?? null;
}

export const DATA_CLASS_LABEL: Record<string, string> = {
  run_artifacts: 'Run artifacts',
  run_steps_content: 'Run step content',
  audit_events: 'Audit events',
  usage_events: 'Usage events',
  policy_decisions: 'Policy decisions',
};

// ---- Date inputs ----------------------------------------------------------------------------------

export const startOfDay = (d: string): string | undefined =>
  d ? new Date(`${d}T00:00:00`).toISOString() : undefined;
export const endOfDay = (d: string): string | undefined =>
  d ? new Date(`${d}T23:59:59.999`).toISOString() : undefined;
