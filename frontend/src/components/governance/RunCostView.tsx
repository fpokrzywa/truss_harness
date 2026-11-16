'use client';

import Link from 'next/link';
import { useMemo } from 'react';
import type { UsageEvent } from '@/lib/api/governance-types';
import { useRunUsage } from '@/lib/governance-hooks';
import {
  costKindLabel,
  eventCostTotals,
  formatCount,
  formatDuration,
  formatMoney,
  snapshotVersion,
} from '@/lib/governance';
import { orgHref } from '@/lib/scope';
import { EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { CostKindList } from './cost';

const stepOf = (e: UsageEvent): string => e.step_name ?? e.step_id ?? '(run level)';

/** Events ordered by step (first appearance) then attempt: every attempt is its own row, never merged. */
export function orderEvents(events: readonly UsageEvent[]): UsageEvent[] {
  const first = new Map<string, number>();
  events.forEach((e, i) => {
    const k = stepOf(e);
    if (!first.has(k)) first.set(k, i);
  });
  return [...events].sort(
    (a, b) =>
      (first.get(stepOf(a)) ?? 0) - (first.get(stepOf(b)) ?? 0) ||
      (a.attempt ?? 0) - (b.attempt ?? 0) ||
      a.occurred_at.localeCompare(b.occurred_at),
  );
}

function CostCell({ e }: { e: UsageEvent }) {
  const label = costKindLabel(e.cost_kind, {
    versions: e.cost_kind === 'calculated' ? [snapshotVersion(e) ?? ''].filter(Boolean) : undefined,
    count: 1,
  });
  return (
    <>
      {e.cost_kind !== 'unmeasured' && typeof e.cost_amount === 'number' && (
        <span className="font-mono">{formatMoney(e.cost_amount, e.currency)}</span>
      )}
      <span className="block text-xs text-slate-600">{label}</span>
    </>
  );
}

function SnapshotCell({ e }: { e: UsageEvent }) {
  const s = e.rate_snapshot;
  if (!s && !e.rate_card_version_id) return <span className="text-slate-600">none</span>;
  return (
    <details className="text-xs">
      <summary className="cursor-pointer">Rate snapshot {snapshotVersion(e) ?? ''}</summary>
      <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-2 font-mono">
        {e.rate_card_version_id && (
          <>
            <dt>rate_card_version_id</dt>
            <dd className="break-all">{e.rate_card_version_id}</dd>
          </>
        )}
        {Object.entries(s ?? {}).map(([k, v]) => (
          <div key={k} className="contents">
            <dt>{k}</dt>
            <dd className="break-all">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/** Workflow Cost Detail: usage events of one run, per step and attempt, with rate snapshots. */
export function RunCostView({ id }: { id: string }) {
  const org = useOrgParam();
  const usage = useRunUsage(org, id);
  return (
    <div className="space-y-6">
      <PageHeader
        title="Workflow cost detail"
        description={`Per-step and per-attempt usage for run ${id}. Retries are separate attempts and are never merged.`}
        actions={
          <Link className="btn" href={`${orgHref(org)}/runs/${encodeURIComponent(id)}`}>
            Open run
          </Link>
        }
      />
      <QueryGate query={usage} noun="run cost">
        {(events) => <RunCostBody events={events} />}
      </QueryGate>
    </div>
  );
}

function RunCostBody({ events }: { events: UsageEvent[] }) {
  const ordered = useMemo(() => orderEvents(events), [events]);
  const totals = useMemo(() => eventCostTotals(events), [events]);
  const currency = events.find((e) => e.currency)?.currency;
  const versions = [
    ...new Set(
      events
        .filter((e) => e.cost_kind === 'calculated')
        .map((e) => snapshotVersion(e))
        .filter((v): v is string => !!v),
    ),
  ];
  if (events.length === 0)
    return (
      <section className="card p-0">
        <EmptyState>No usage events were recorded for this run.</EmptyState>
      </section>
    );
  return (
    <>
      <section aria-label="Run cost summary" className="card space-y-2">
        <h2 className="text-sm font-semibold">
          Run total by cost kind ({events.length} event{events.length === 1 ? '' : 's'})
        </h2>
        <CostKindList cost={totals} currency={currency} versions={versions} />
      </section>
      <section aria-label="Usage events by step and attempt" className="card overflow-x-auto p-0">
        <table className="w-full">
          <caption className="sr-only">Usage events by step and attempt</caption>
          <thead className="border-b border-slate-200">
            <tr>
              {[
                'Step',
                'Attempt',
                'Event',
                'Model',
                'Input tokens',
                'Output tokens',
                'Duration',
                'Cost (with kind)',
                'Rate snapshot',
              ].map((h) => (
                <th key={h} scope="col" className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {ordered.map((e) => (
              <tr key={e.id}>
                <th scope="row" className="td text-left font-normal">
                  {stepOf(e)}
                </th>
                <td className="td">
                  Attempt {e.attempt ?? 1}
                  {e.corrects_event_id && (
                    <span className="block text-xs text-amber-900">
                      Correction of {e.corrects_event_id}
                    </span>
                  )}
                </td>
                <td className="td">
                  {e.event_type}
                  <span className="block text-xs text-slate-600">{formatDate(e.occurred_at)}</span>
                </td>
                <td className="td">{e.model_id ?? e.provider_key ?? '-'}</td>
                <td className="td">{formatCount(e.input_tokens ?? 0)}</td>
                <td className="td">{formatCount(e.output_tokens ?? 0)}</td>
                <td className="td">
                  {e.duration_ms != null ? formatDuration(e.duration_ms) : '-'}
                </td>
                <td className="td">
                  <CostCell e={e} />
                </td>
                <td className="td">
                  <SnapshotCell e={e} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
