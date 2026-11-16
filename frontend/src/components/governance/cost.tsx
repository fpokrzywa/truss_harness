'use client';

import Link from 'next/link';
import type {
  CostBreakdown,
  GroupBy,
  Reconciliation,
  UsageGroup,
  UsageTotals,
} from '@/lib/api/governance-types';
import {
  costKindLabel,
  formatCount,
  formatDuration,
  formatMoney,
  TOTAL_KNOWN_LABEL,
  TOTAL_KNOWN_NOTE,
} from '@/lib/governance';
import { governHref } from '@/lib/scope';
import { Badge } from '../admin/ui';

/**
 * Per-kind cost figures with the explicit labels of contract section 0, then "Total known". There is no
 * unlabeled total: the sum always carries its own definition.
 */
export function CostKindList({
  cost,
  currency,
  versions,
  label = 'Cost by kind',
}: {
  cost: CostBreakdown;
  currency?: string | null;
  versions?: (string | number)[];
  label?: string;
}) {
  return (
    <dl
      aria-label={label}
      className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[minmax(0,2fr)_1fr]"
    >
      {(['provider_reported', 'calculated', 'estimated_infra'] as const).map((k) => (
        <div key={k} className="contents">
          <dt className="text-slate-700">{costKindLabel(k, { versions })}</dt>
          <dd className="font-mono">{formatMoney(cost[k], currency)}</dd>
        </div>
      ))}
      <div className="contents">
        <dt className="text-slate-700">
          {costKindLabel('unmeasured', { count: cost.unmeasured_count })}
        </dt>
        <dd className="text-slate-600">no amount</dd>
      </div>
      <div className="contents">
        <dt className="font-semibold text-slate-900" title={TOTAL_KNOWN_NOTE}>
          {TOTAL_KNOWN_LABEL}
        </dt>
        <dd className="font-mono font-semibold">{formatMoney(cost.total_known, currency)}</dd>
      </div>
      <p className="text-xs text-slate-600 sm:col-span-2">{TOTAL_KNOWN_NOTE}</p>
    </dl>
  );
}

export function ReconciliationBadge({ reconciliation }: { reconciliation: Reconciliation }) {
  const ok = reconciliation.consistent;
  return (
    <span
      className="inline-flex flex-wrap items-center gap-2 text-sm"
      title={`Events sum ${reconciliation.events_sum} tokens; rollup sum ${reconciliation.rollup_sum} tokens`}
    >
      <Badge tone={ok ? 'good' : 'bad'}>
        {ok ? 'Reconciled: consistent' : 'Reconciliation mismatch'}
      </Badge>
      <span className="text-xs text-slate-600">
        events {reconciliation.events_sum} tokens, rollups {reconciliation.rollup_sum} tokens
        {reconciliation.mismatched_rows !== undefined &&
          `, ${reconciliation.mismatched_rows} mismatched row${reconciliation.mismatched_rows === 1 ? '' : 's'}`}
        {reconciliation.level &&
          ` (${reconciliation.level === 'full' ? 'every rollup row compared' : 'totals compared only'})`}
      </span>
    </span>
  );
}

const rowKey = (g: UsageGroup, i: number) => `${g.key}-${i}`;

/** Usage groups with a labelled money column per cost kind. */
export function CostTable({
  org,
  groups,
  totals,
  groupBy,
  caption,
}: {
  org: string;
  groups: UsageGroup[];
  totals: UsageTotals;
  groupBy: GroupBy;
  caption: string;
}) {
  const versions = totals.rate_card_versions;
  const unit = (g: { currency?: string | null }) => g.currency ?? totals.currency;
  const headers = [
    groupBy,
    'Calls',
    'Input tokens',
    'Output tokens',
    'Cached tokens',
    'Duration',
    costKindLabel('provider_reported'),
    costKindLabel('calculated', { versions }),
    costKindLabel('estimated_infra'),
    'Unmeasured events',
    `${TOTAL_KNOWN_LABEL} (not an invoice)`,
  ];
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {headers.map((h, i) => (
              <th key={h} scope="col" className={`th ${i === 0 ? 'capitalize' : ''}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {groups.map((g, i) => (
            <tr key={rowKey(g, i)}>
              <th scope="row" className="td text-left font-normal">
                {groupBy === 'run' ? (
                  <Link
                    className="font-mono text-xs text-blue-800 underline"
                    href={`${governHref(org, 'runs')}/${encodeURIComponent(g.key)}/cost`}
                  >
                    {g.key}
                  </Link>
                ) : (
                  (g.label ?? g.key)
                )}
              </th>
              <CostCells g={g} currency={unit(g)} />
            </tr>
          ))}
        </tbody>
        <tfoot className="border-t border-slate-300 font-semibold">
          <tr>
            <th scope="row" className="td text-left">
              All groups
            </th>
            <CostCells g={totals} currency={totals.currency} />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function CostCells({
  g,
  currency,
}: {
  g: Pick<
    UsageTotals,
    'calls' | 'input_tokens' | 'output_tokens' | 'cached_tokens' | 'duration_ms' | 'cost'
  >;
  currency?: string | null;
}) {
  return (
    <>
      <td className="td">{formatCount(g.calls)}</td>
      <td className="td">{formatCount(g.input_tokens)}</td>
      <td className="td">{formatCount(g.output_tokens)}</td>
      <td className="td">{formatCount(g.cached_tokens)}</td>
      <td className="td">{formatDuration(g.duration_ms)}</td>
      <td className="td font-mono">{formatMoney(g.cost.provider_reported, currency)}</td>
      <td className="td font-mono">{formatMoney(g.cost.calculated, currency)}</td>
      <td className="td font-mono">{formatMoney(g.cost.estimated_infra, currency)}</td>
      <td className="td">{formatCount(g.cost.unmeasured_count)}</td>
      <td className="td font-mono">{formatMoney(g.cost.total_known, currency)}</td>
    </>
  );
}
