'use client';

import { useMemo, useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { UsageSummary } from '@/lib/api/governance-types';
import { useCan } from '@/lib/hooks';
import { useUsageEvents, useUsageSummary } from '@/lib/governance-hooks';
import {
  costKindLabel,
  dailyTokens,
  formatCount,
  formatDuration,
  formatMoney,
  TOTAL_KNOWN_LABEL,
} from '@/lib/governance';
import { EmptyState, PageHeader, QueryGate } from '../admin/ui';
import { QueryError } from '../QueryError';
import { useOrgParam } from '../security/useOrg';
import { ChartFrame, StackedCostBars, TokenLines } from './charts';
import { CostKindList, CostTable, ReconciliationBadge } from './cost';
import { initialFilters, toUsageFilters, UsageFilterBar, type FilterState } from './UsageFilterBar';

/** Token Usage: tokens by any contract dimension plus a daily series built from the usage ledger. */
export function UsageView() {
  const org = useOrgParam();
  const [f, setF] = useState<FilterState>(initialFilters('model'));
  const filters = toUsageFilters(f);
  const summary = useUsageSummary(org, filters);
  const events = useUsageEvents(org, {
    from: filters.from,
    to: filters.to,
  });
  const loaded = useMemo(() => events.data?.pages.flatMap((p) => p.items) ?? [], [events.data]);
  const points = useMemo(() => dailyTokens(loaded), [loaded]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Token usage"
        description="Input, output and cached tokens by dimension. Token counts are measurements; money figures are on the Cost Explorer with their cost kind."
      />
      <UsageFilterBar org={org} value={f} onChange={setF} />

      <QueryGate query={summary} noun="usage">
        {(s) => (
          <>
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span>
                {formatCount(s.totals.input_tokens)} input, {formatCount(s.totals.output_tokens)}{' '}
                output, {formatCount(s.totals.cached_tokens)} cached tokens in{' '}
                {formatCount(s.totals.calls)} calls
              </span>
              <ReconciliationBadge reconciliation={s.reconciliation} />
            </div>
            {points.length > 0 && (
              <ChartFrame
                title="Daily tokens"
                summary={`Input and output tokens per UTC day from ${loaded.length} loaded ledger events${
                  events.hasNextPage ? ' (more events exist; load more to extend the series)' : ''
                }.`}
                chart={<TokenLines points={points} />}
                table={
                  <table className="w-full">
                    <caption className="sr-only">Daily tokens</caption>
                    <thead>
                      <tr>
                        {['Day (UTC)', 'Input tokens', 'Output tokens', 'Events'].map((h) => (
                          <th key={h} scope="col" className="th">
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {points.map((p) => (
                        <tr key={p.day}>
                          <th scope="row" className="td text-left font-normal">
                            {p.day}
                          </th>
                          <td className="td">{formatCount(p.input_tokens)}</td>
                          <td className="td">{formatCount(p.output_tokens)}</td>
                          <td className="td">{formatCount(p.events)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                }
              />
            )}
            {events.hasNextPage && (
              <button
                type="button"
                className="btn"
                disabled={events.isFetchingNextPage}
                onClick={() => void events.fetchNextPage()}
              >
                {events.isFetchingNextPage ? 'Loading...' : 'Load more events'}
              </button>
            )}
            {events.isError && <QueryError error={events.error} />}
            <TokenTable summary={s} groupBy={f.groupBy} />
          </>
        )}
      </QueryGate>
    </div>
  );
}

function TokenTable({ summary, groupBy }: { summary: UsageSummary; groupBy: string }) {
  if (summary.groups.length === 0)
    return (
      <section className="card p-0">
        <EmptyState>No usage in this range.</EmptyState>
      </section>
    );
  return (
    <section aria-label="Token usage table" className="card overflow-x-auto p-0">
      <table className="w-full">
        <caption className="sr-only">Token usage by {groupBy}</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {[groupBy, 'Calls', 'Input tokens', 'Output tokens', 'Cached tokens', 'Duration'].map(
              (h, i) => (
                <th key={h} scope="col" className={`th ${i === 0 ? 'capitalize' : ''}`}>
                  {h}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {summary.groups.map((g, i) => (
            <tr key={`${g.key}-${i}`}>
              <th scope="row" className="td text-left font-normal">
                {g.label ?? g.key}
              </th>
              <td className="td">{formatCount(g.calls)}</td>
              <td className="td">{formatCount(g.input_tokens)}</td>
              <td className="td">{formatCount(g.output_tokens)}</td>
              <td className="td">{formatCount(g.cached_tokens)}</td>
              <td className="td">{formatDuration(g.duration_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** Cost Explorer: group-by and filters, per-cost-kind stacked bars, labelled table, gated CSV export. */
export function CostExplorerView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const [f, setF] = useState<FilterState>(initialFilters('project'));
  const filters = toUsageFilters(f);
  const summary = useUsageSummary(org, filters);
  const exportUrl = endpoints.usageExportUrl(org, filters);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Cost explorer"
        description="Costs are reported per kind and are never an invoice: calculated costs come from rate cards, provider-reported costs from the provider, infrastructure costs are estimates."
        actions={
          can('data:export') ? (
            <a className="btn" href={exportUrl} download>
              Export CSV
            </a>
          ) : undefined
        }
      />
      <UsageFilterBar org={org} value={f} onChange={setF} costKind />

      <QueryGate query={summary} noun="costs">
        {(s) => (
          <>
            <section aria-label="Cost summary" className="card space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold">All groups in range</h2>
                <ReconciliationBadge reconciliation={s.reconciliation} />
              </div>
              <CostKindList
                cost={s.totals.cost}
                currency={s.totals.currency}
                versions={s.totals.rate_card_versions}
              />
            </section>
            {s.groups.length === 0 ? (
              <section className="card p-0">
                <EmptyState>No usage in this range.</EmptyState>
              </section>
            ) : (
              <>
                <ChartFrame
                  title={`Known cost by ${f.groupBy}, stacked by cost kind`}
                  summary="Each bar is one group; segments are provider-reported, calculated and estimated infrastructure amounts. Unmeasured events have no amount."
                  chart={
                    <StackedCostBars
                      groups={s.groups}
                      currency={s.totals.currency}
                      ratesVersions={s.totals.rate_card_versions}
                    />
                  }
                  table={
                    <ChartData
                      groups={s.groups}
                      currency={s.totals.currency}
                      versions={s.totals.rate_card_versions}
                    />
                  }
                />
                <section aria-label="Cost table" className="card p-0">
                  <CostTable
                    org={org}
                    groups={s.groups}
                    totals={s.totals}
                    groupBy={f.groupBy}
                    caption={`Cost by ${f.groupBy}`}
                  />
                </section>
              </>
            )}
          </>
        )}
      </QueryGate>
    </div>
  );
}

function ChartData({
  groups,
  currency,
  versions,
}: {
  groups: UsageSummary['groups'];
  currency?: string | null;
  versions?: (string | number)[];
}) {
  const heads = [
    'Group',
    costKindLabel('provider_reported'),
    costKindLabel('calculated', { versions }),
    costKindLabel('estimated_infra'),
    `${TOTAL_KNOWN_LABEL} (not an invoice)`,
  ];
  return (
    <table className="w-full">
      <caption className="sr-only">Chart data: known cost by group and cost kind</caption>
      <thead>
        <tr>
          {heads.map((h) => (
            <th key={h} scope="col" className="th">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {groups.map((g, i) => (
          <tr key={`${g.key}-${i}`}>
            <th scope="row" className="td text-left font-normal">
              {g.label ?? g.key}
            </th>
            <td className="td font-mono">{formatMoney(g.cost.provider_reported, currency)}</td>
            <td className="td font-mono">{formatMoney(g.cost.calculated, currency)}</td>
            <td className="td font-mono">{formatMoney(g.cost.estimated_infra, currency)}</td>
            <td className="td font-mono">{formatMoney(g.cost.total_known, currency)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Model Consumption: the cost table grouped by model (labels per kind, reconciliation shown). */
export function ModelConsumptionView() {
  const org = useOrgParam();
  const [f, setF] = useState<FilterState>(initialFilters('model'));
  const summary = useUsageSummary(org, toUsageFilters({ ...f, groupBy: 'model' }));
  return (
    <div className="space-y-6">
      <PageHeader
        title="Model consumption"
        description="Calls, tokens and cost per model. Costs are per kind and never an invoice."
      />
      <UsageFilterBar org={org} value={f} onChange={setF} groupBy={false} />
      <QueryGate query={summary} noun="model consumption">
        {(s) => (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <ReconciliationBadge reconciliation={s.reconciliation} />
            </div>
            {s.groups.length === 0 ? (
              <section className="card p-0">
                <EmptyState>No model usage in this range.</EmptyState>
              </section>
            ) : (
              <section aria-label="Model consumption table" className="card p-0">
                <CostTable
                  org={org}
                  groups={s.groups}
                  totals={s.totals}
                  groupBy="model"
                  caption="Consumption by model"
                />
              </section>
            )}
          </>
        )}
      </QueryGate>
    </div>
  );
}
