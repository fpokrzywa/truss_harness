import type { ApiClient } from './client';
import type {
  Budget,
  BudgetInput,
  BudgetStatus,
  DecisionFilters,
  EventFilters,
  GovernanceOverview,
  HealthDetails,
  GovernancePolicy,
  GovernancePolicyInput,
  PerfGroupBy,
  PerfRow,
  PolicyDecision,
  Quota,
  QuotaInput,
  RateCard,
  RateCardInput,
  RateCardVersion,
  RateCardVersionInput,
  RetentionPreview,
  RetentionRow,
  RetentionSettings,
  UsageEvent,
  UsageFilters,
  UsageSummary,
} from './governance-types';
import type { ListResponse } from './types';
import {
  normalizeOverview,
  normalizePerf,
  normalizeRetention,
  normalizeRetentionPreview,
  normalizeSummary,
} from '../governance';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
const asItems = <T>(res: ListResponse<T> | T[] | undefined | null): T[] =>
  Array.isArray(res) ? res : (res?.items ?? []);

type Query = Record<string, string | number | undefined>;

/** Query for usage/summary and usage/export: empty filters are omitted by the client. */
export function usageQuery(f: UsageFilters): Query {
  return {
    group_by: f.group_by,
    from: f.from,
    to: f.to,
    workspace: f.workspace,
    project: f.project,
    cost_kind: f.cost_kind || undefined,
  };
}

const retentionBody = (rows: RetentionRow[]) => ({
  items: rows.map((r) => ({
    data_class: r.data_class,
    retain_days: r.retain_days,
    legal_hold: r.legal_hold,
  })),
});

/** Phase 8 routes (docs/api/PHASE_8_CONTRACT.md). */
export function createGovernanceEndpoints(client: ApiClient) {
  const send = <T>(method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, body?: unknown) =>
    client.request<T>(path, { method, ...(body === undefined ? {} : { body }) });
  const get = <T>(path: string, query?: Query) => client.request<T>(path, { query });

  return {
    usageSummary: async (org: string, f: UsageFilters): Promise<UsageSummary> =>
      // The server default (`reconcile=totals`) compares control totals per day and cost kind and
      // stays fast on large tenants (NFR-PERF-003); `level` tells the UI which check ran. The full
      // per-row comparison (`reconcile=full`) is an explicit, slow operation and is not used here.
      normalizeSummary(await get<unknown>(`${org$(org)}/usage/summary`, usageQuery(f))),
    listUsageEvents: (org: string, f: EventFilters = {}) =>
      get<ListResponse<UsageEvent>>(`${org$(org)}/usage/events`, { limit: 200, ...f }),
    runUsage: async (org: string, runId: string): Promise<UsageEvent[]> => {
      const res = await get<unknown>(`${org$(org)}/usage/runs/${enc(runId)}`);
      if (Array.isArray(res)) return res as UsageEvent[];
      const r = res as { events?: UsageEvent[]; items?: UsageEvent[] } | null;
      return r?.events ?? r?.items ?? [];
    },
    /** Same-origin URL for the audited CSV export (a plain navigation download; needs data:export). */
    usageExportUrl: (org: string, f: UsageFilters): string => {
      const params = new URLSearchParams({ format: 'csv' });
      for (const [k, v] of Object.entries(usageQuery(f))) if (v) params.set(k, String(v));
      return `${org$(org)}/usage/export?${params.toString()}`;
    },

    listRateCards: async (org: string) =>
      asItems(await get<ListResponse<RateCard> | RateCard[]>(`${org$(org)}/rate-cards`)),
    createRateCard: (org: string, input: RateCardInput) =>
      send<RateCard>('POST', `${org$(org)}/rate-cards`, input),
    listPlatformRateCards: async () =>
      asItems(await get<ListResponse<RateCard> | RateCard[]>('/api/v1/platform/rate-cards')),
    createPlatformRateCard: (input: RateCardInput) =>
      send<RateCard>('POST', '/api/v1/platform/rate-cards', input),
    listRateCardVersions: async (base: string, id: string) =>
      asItems(
        await get<ListResponse<RateCardVersion> | RateCardVersion[]>(
          `${base}/rate-cards/${enc(id)}/versions`,
        ),
      ),
    createRateCardVersion: (base: string, id: string, input: RateCardVersionInput) =>
      send<RateCardVersion>('POST', `${base}/rate-cards/${enc(id)}/versions`, input),

    listBudgets: async (org: string) =>
      asItems(await get<ListResponse<Budget> | Budget[]>(`${org$(org)}/budgets`)),
    createBudget: (org: string, input: BudgetInput) =>
      send<Budget>('POST', `${org$(org)}/budgets`, input),
    /** `scope` and `metric` are immutable server-side (a change is a 422), so they are not sent. */
    updateBudget: (org: string, b: Budget, input: BudgetInput) => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars -- dropped on purpose
      const { scope, metric, ...mutable } = input;
      return send<Budget>('PATCH', `${org$(org)}/budgets/${enc(b.id)}`, {
        ...mutable,
        ...(b.row_version !== undefined ? { row_version: b.row_version } : {}),
      });
    },
    budgetStatus: (org: string, id: string) =>
      get<BudgetStatus>(`${org$(org)}/budgets/${enc(id)}/status`),
    listQuotas: async (org: string) =>
      asItems(await get<ListResponse<Quota> | Quota[]>(`${org$(org)}/quotas`)),
    createQuota: (org: string, input: QuotaInput) =>
      send<Quota>('POST', `${org$(org)}/quotas`, input),
    updateQuota: (org: string, q: Quota, input: QuotaInput) =>
      send<Quota>('PATCH', `${org$(org)}/quotas/${enc(q.id)}`, {
        limit: input.limit,
        ...(q.row_version !== undefined ? { row_version: q.row_version } : {}),
      }),

    governanceOverview: async (org: string): Promise<GovernanceOverview> =>
      normalizeOverview(await get<unknown>(`${org$(org)}/governance/overview`)),
    listGovPolicies: async (org: string) =>
      asItems(
        await get<ListResponse<GovernancePolicy> | GovernancePolicy[]>(
          `${org$(org)}/governance/policies`,
        ),
      ),
    createGovPolicy: (org: string, input: GovernancePolicyInput) =>
      send<GovernancePolicy>('POST', `${org$(org)}/governance/policies`, input),
    /** `kind` and `scope` are immutable server-side, so only name, definition and status are sent. */
    updateGovPolicy: (org: string, p: GovernancePolicy, input: GovernancePolicyInput) =>
      send<GovernancePolicy>('PATCH', `${org$(org)}/governance/policies/${enc(p.id)}`, {
        ...(input.name !== undefined ? { name: input.name } : {}),
        definition: input.definition,
        status: input.status,
        ...(p.row_version !== undefined ? { row_version: p.row_version } : {}),
      }),
    getRetention: async (org: string): Promise<RetentionSettings> =>
      normalizeRetention(await get<unknown>(`${org$(org)}/governance/retention`)),
    putRetention: async (org: string, rows: RetentionRow[]): Promise<RetentionSettings> =>
      normalizeRetention(
        await send<unknown>('PUT', `${org$(org)}/governance/retention`, retentionBody(rows)),
      ),
    /** Counts only; nothing is deleted by this call (and retention is not enforced at all yet). */
    previewRetention: async (org: string, rows: RetentionRow[]): Promise<RetentionPreview> =>
      normalizeRetentionPreview(
        await send<unknown>(
          'POST',
          `${org$(org)}/governance/retention/preview`,
          retentionBody(rows),
        ),
      ),
    listDecisions: (org: string, f: DecisionFilters = {}) =>
      get<ListResponse<PolicyDecision>>(`${org$(org)}/governance/decisions`, { limit: 50, ...f }),

    platformHealthDetails: () => get<HealthDetails>('/api/v1/platform/health-details'),

    performance: async (
      org: string,
      f: { group_by: PerfGroupBy; from?: string; to?: string },
    ): Promise<PerfRow[]> =>
      normalizePerf(await get<unknown>(`${org$(org)}/observability/performance`, f)),
  };
}
