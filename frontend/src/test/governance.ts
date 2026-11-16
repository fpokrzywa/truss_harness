import { ALL_PERMS, list, stubApi } from './security';
import type { Handler } from './utils';

export const GOV_PERMS = [
  ...ALL_PERMS,
  'usage:read',
  'budget:read',
  'budget:manage',
  'governance:read',
  'governance:manage',
  'pricing:read',
  'pricing:manage',
  'ops:read',
];

export const COST = {
  provider_reported: 1.5,
  calculated: 2.25,
  estimated_infra: 0.25,
  unmeasured_count: 3,
  total_known: 4,
};

export const SUMMARY = {
  groups: [
    {
      key: 'proj-1',
      label: 'API project',
      input_tokens: 1000,
      output_tokens: 500,
      cached_tokens: 100,
      calls: 12,
      duration_ms: 3400,
      cost: COST,
      currency: 'USD',
    },
  ],
  totals: {
    input_tokens: 1000,
    output_tokens: 500,
    cached_tokens: 100,
    calls: 12,
    duration_ms: 3400,
    cost: COST,
    currency: 'USD',
    rate_card_versions: [2],
  },
  reconciliation: { events_sum: 4, rollup_sum: 4, consistent: true },
};

/** stubApi with Phase 8 permissions and empty scope lookups used by the filter/scope pickers. */
export function stubGov(routes: Record<string, Handler> = {}, perms: string[] = GOV_PERMS) {
  return stubApi(
    {
      'GET /api/v1/orgs/acme/users': () => list([]),
      'GET /api/v1/orgs/acme/agents': () => list([]),
      'GET /api/v1/orgs/acme/pipelines': () => list([]),
      ...routes,
    },
    perms,
  );
}

export const queryOf = (calls: { key: string; url: string }[], key: string) => {
  const c = calls.filter((x) => x.key === key).at(-1);
  return new URL(c?.url ?? '', 'http://x').searchParams;
};
