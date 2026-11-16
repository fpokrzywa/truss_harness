'use client';

import { useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type {
  PerfGroupBy,
  PolicyKind,
  RetentionPreviewRow,
  RetentionRow,
  RetentionSettings,
} from '@/lib/api/governance-types';
import { useCan } from '@/lib/hooks';
import {
  govKey,
  useDecisions,
  useGovOverview,
  usePerformance,
  useRetention,
} from '@/lib/governance-hooks';
import {
  DATA_CLASS_LABEL,
  endOfDay,
  floorFor,
  formatCount,
  formatDuration,
  formatMoney,
  formatPercent,
  RETENTION_NOT_ENFORCED_TEXT,
  startOfDay,
  TOTAL_KNOWN_LABEL,
} from '@/lib/governance';
import { governHref, orgHref } from '@/lib/scope';
import {
  ActionError,
  EmptyState,
  formatDate,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { QueryError } from '../QueryError';
import { useOrgParam } from '../security/useOrg';
import { CostKindList } from './cost';
import { UtilizationBar } from './BudgetsView';
import { KIND_LABEL } from './PoliciesView';

/** Governance Overview: budgets, top spenders, denials, pending approvals, policy counts. */
export function GovernanceOverviewView() {
  const org = useOrgParam();
  const overview = useGovOverview(org);
  return (
    <div className="space-y-6">
      <PageHeader
        title="Governance overview"
        description="Budget status, top spenders, recent denials, pending approvals and policy counts."
      />
      <QueryGate query={overview} noun="overview">
        {(o) => (
          <div className="grid gap-4 lg:grid-cols-2">
            <section aria-label="Denials and approvals" className="card">
              <h2 className="text-sm font-semibold">Last 7 days</h2>
              <dl className="mt-2 grid grid-cols-2 gap-3">
                <div>
                  <dt className="text-xs text-slate-600">Budget and policy denials</dt>
                  <dd className="text-2xl font-semibold">{formatCount(o.denials_7d)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-600">Pending approvals</dt>
                  <dd className="text-2xl font-semibold">
                    <Link className="text-blue-800 underline" href={`${orgHref(org)}/approvals`}>
                      {formatCount(o.pending_approvals)}
                    </Link>
                  </dd>
                </div>
              </dl>
            </section>
            <section aria-label="Policy counts" className="card">
              <h2 className="text-sm font-semibold">Policies</h2>
              {Object.keys(o.policy_counts).length === 0 ? (
                <p className="mt-2 text-sm text-slate-600">No policies.</p>
              ) : (
                <ul className="mt-2 space-y-1 text-sm">
                  {Object.entries(o.policy_counts).map(([k, n]) => (
                    <li key={k} className="flex justify-between">
                      <Link className="text-blue-800 underline" href={governHref(org, 'policies')}>
                        {KIND_LABEL[k as PolicyKind] ?? k}
                      </Link>
                      <span>{formatCount(n)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section aria-label="Budget status" className="card">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold">Budgets</h2>
                <Link className="text-sm text-blue-800 underline" href={governHref(org, 'budgets')}>
                  Manage budgets
                </Link>
              </div>
              {o.budgets.length === 0 ? (
                <p className="mt-2 text-sm text-slate-600">No budgets defined.</p>
              ) : (
                <ul className="mt-2 space-y-3">
                  {o.budgets.map((b) => (
                    <li key={b.id}>
                      <p className="text-sm">{b.label ?? b.id}</p>
                      <UtilizationBar
                        utilization={b.utilization}
                        thresholds={b.alert_thresholds ?? []}
                        label={`Utilization of ${b.label ?? b.id}`}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section aria-label="Top spenders" className="card">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold">Top spenders</h2>
                <Link className="text-sm text-blue-800 underline" href={governHref(org, 'costs')}>
                  Cost explorer
                </Link>
              </div>
              {o.top_spenders.length === 0 ? (
                <p className="mt-2 text-sm text-slate-600">No spend recorded.</p>
              ) : (
                <ul className="mt-2 space-y-3">
                  {o.top_spenders.map((s) => (
                    <li key={s.key}>
                      <p className="text-sm font-medium">{s.label ?? s.key}</p>
                      {s.cost ? (
                        <CostKindList
                          cost={s.cost}
                          currency={s.currency}
                          label={`Cost by kind for ${s.label ?? s.key}`}
                        />
                      ) : (
                        <p className="text-sm">
                          <span className="text-slate-700">{TOTAL_KNOWN_LABEL}: </span>
                          <span className="font-mono">
                            {formatMoney(s.cost_total_known, s.currency)}
                          </span>
                        </p>
                      )}
                      {s.tokens !== undefined && (
                        <p className="text-xs text-slate-600">{formatCount(s.tokens)} tokens</p>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </QueryGate>
    </div>
  );
}

/** Retention settings per data class with platform floors and a count-only preview. No deletion here. */
export function RetentionView() {
  const org = useOrgParam();
  const { can } = useCan(org);
  const manage = can('governance:manage');
  const retention = useRetention(org);
  return (
    <div className="space-y-6">
      <PageHeader
        title="Data retention"
        description="How long each data class is meant to be kept. Floors are set by the platform; legal hold is recorded per data class."
      />
      <QueryGate query={retention} noun="retention settings">
        {(settings) => (
          <>
            <RetentionNotice enforcement={settings.enforcement} note={settings.note} />
            <RetentionForm
              key={JSON.stringify(settings.items)}
              org={org}
              rows={settings.items}
              manage={manage}
            />
          </>
        )}
      </QueryGate>
    </div>
  );
}

/** Honesty banner: the server reports whether retention is actually enforced. */
function RetentionNotice({ enforcement, note }: Pick<RetentionSettings, 'enforcement' | 'note'>) {
  if (enforcement === 'enforced') return null;
  return (
    <div role="note" className="card border-amber-300 bg-amber-50 text-sm text-amber-900">
      <p className="font-semibold">Not enforced</p>
      <p>{RETENTION_NOT_ENFORCED_TEXT}</p>
      {note && <p className="mt-1 text-xs">{note}</p>}
    </div>
  );
}

function RetentionForm({
  org,
  rows,
  manage,
}: {
  org: string;
  rows: RetentionRow[];
  manage: boolean;
}) {
  const qc = useQueryClient();
  const save = useAction();
  const preview = useAction();
  const [edit, setEdit] = useState<RetentionRow[]>(rows);
  const [counts, setCounts] = useState<{
    items: RetentionPreviewRow[];
    enforcement: string;
    note: string | null;
  } | null>(null);
  const [saved, setSaved] = useState(false);

  const patch = (i: number, change: Partial<RetentionRow>) => {
    setSaved(false);
    setCounts(null);
    setEdit((cur) => cur.map((r, j) => (j === i ? { ...r, ...change } : r)));
  };
  const errors = edit.map((r) => {
    const floor = floorFor(r);
    if (!Number.isInteger(r.retain_days) || r.retain_days < 1)
      return 'Enter a whole number of days.';
    if (floor !== null && r.retain_days < floor)
      return `Cannot be below the platform floor of ${floor} days.`;
    return null;
  });
  const valid = errors.every((e) => e === null);

  async function runPreview() {
    const r = await preview.run(() => endpoints.previewRetention(org, edit));
    if (r.ok) setCounts(r.value);
  }
  async function runSave() {
    const r = await save.run(() => endpoints.putRetention(org, edit));
    if (r.ok) {
      setSaved(true);
      await qc.invalidateQueries({ queryKey: govKey(org, 'retention') });
    }
  }

  if (rows.length === 0)
    return (
      <section className="card p-0">
        <EmptyState>No retention settings are available.</EmptyState>
      </section>
    );

  return (
    <div className="space-y-4">
      <section aria-label="Retention settings" className="card overflow-x-auto p-0">
        <table className="w-full">
          <caption className="sr-only">Retention settings by data class</caption>
          <thead className="border-b border-slate-200">
            <tr>
              {['Data class', 'Retain (days)', 'Platform floor', 'Legal hold'].map((h) => (
                <th key={h} scope="col" className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {edit.map((r, i) => {
              const name = DATA_CLASS_LABEL[r.data_class] ?? r.data_class;
              const floor = floorFor(r);
              return (
                <tr key={r.data_class}>
                  <th scope="row" className="td text-left font-normal">
                    {name}
                  </th>
                  <td className="td">
                    <input
                      type="number"
                      min={floor ?? 1}
                      aria-label={`${name} retain days`}
                      aria-invalid={errors[i] ? true : undefined}
                      className="input w-28"
                      disabled={!manage}
                      value={Number.isNaN(r.retain_days) ? '' : r.retain_days}
                      onChange={(e) => patch(i, { retain_days: e.target.valueAsNumber })}
                    />
                    {errors[i] && <p className="mt-1 text-xs text-red-700">{errors[i]}</p>}
                  </td>
                  <td className="td text-xs text-slate-600">
                    {floor !== null ? `At least ${floor} days` : 'None set'}
                  </td>
                  <td className="td">
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        disabled={!manage}
                        checked={r.legal_hold}
                        onChange={(e) => patch(i, { legal_hold: e.target.checked })}
                      />
                      Legal hold on {name}
                    </label>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn"
          disabled={!valid || preview.busy}
          onClick={() => void runPreview()}
        >
          {preview.busy ? 'Counting...' : 'Preview records older than the cutoff'}
        </button>
        {manage && (
          <button
            type="button"
            className="btn btn-primary"
            disabled={!valid || save.busy}
            onClick={() => void runSave()}
          >
            {save.busy ? 'Saving...' : 'Save retention settings'}
          </button>
        )}
      </div>
      {saved && (
        <p role="status" className="text-sm text-green-800">
          Retention settings saved.
        </p>
      )}
      <ActionError error={preview.error} title="Could not preview" />
      <ActionError error={save.error} title="Could not save retention settings" />
      {counts && (
        <section aria-label="Purge preview" className="card space-y-2">
          <h2 className="text-sm font-semibold">Preview: records older than the cutoff</h2>
          <p className="text-xs text-slate-600">
            Counts only. Nothing is deleted by this page.
            {counts.enforcement !== 'enforced' && ` ${RETENTION_NOT_ENFORCED_TEXT}`}
          </p>
          {counts.note && <p className="text-xs text-slate-600">{counts.note}</p>}
          <ul className="text-sm">
            {counts.items.map((c) => (
              <li key={c.data_class}>
                {DATA_CLASS_LABEL[c.data_class] ?? c.data_class}: {formatCount(c.count)}
                {c.on_legal_hold ? ' (on legal hold)' : ''}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

/** Policy decisions log (execution policy decisions with reason codes and policy ids). */
export function DecisionsView() {
  const org = useOrgParam();
  const [runId, setRunId] = useState('');
  const [decision, setDecision] = useState('');
  const q = useDecisions(org, {
    run_id: runId.trim() || undefined,
    decision: decision.trim() || undefined,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="space-y-6">
      <PageHeader
        title="Policy decisions"
        description="Execution policy decisions with reason codes and the policies that produced them."
      />
      <form
        aria-label="Decision filters"
        className="card grid gap-3 sm:grid-cols-2"
        onSubmit={(e) => e.preventDefault()}
      >
        <TextField label="Run id" value={runId} onChange={setRunId} monospace />
        <TextField
          label="Decision"
          value={decision}
          onChange={setDecision}
          help="For example deny or allow."
        />
      </form>
      {q.isPending && (
        <p role="status" className="text-sm text-slate-600">
          Loading decisions...
        </p>
      )}
      {q.isError && <QueryError error={q.error} />}
      {q.data && (
        <section aria-label="Decisions" className="card overflow-x-auto p-0">
          {items.length === 0 ? (
            <EmptyState>No decisions match.</EmptyState>
          ) : (
            <table className="w-full">
              <caption className="sr-only">Policy decisions</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['When', 'Decision', 'Reason code', 'Policies', 'Run'].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {items.map((d) => (
                  <tr key={d.id}>
                    <td className="td whitespace-nowrap">{formatDate(d.at ?? d.occurred_at)}</td>
                    <td className="td">{d.decision}</td>
                    <td className="td font-mono text-xs">
                      {(d.reason_codes?.length ? d.reason_codes.join(', ') : d.reason_code) ?? '-'}
                    </td>
                    <td className="td font-mono text-xs">{d.policy_ids?.join(', ') || '-'}</td>
                    <td className="td">
                      {d.run_id ? (
                        <Link
                          className="font-mono text-xs text-blue-800 underline"
                          href={`${orgHref(org)}/runs/${encodeURIComponent(d.run_id)}`}
                        >
                          {d.run_id}
                        </Link>
                      ) : (
                        '-'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {q.hasNextPage && (
            <div className="border-t border-slate-200 p-3">
              <button
                type="button"
                className="btn"
                disabled={q.isFetchingNextPage}
                onClick={() => void q.fetchNextPage()}
              >
                {q.isFetchingNextPage ? 'Loading...' : 'Load more'}
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

/** Execution Performance: latency percentiles and error rates by agent, model or pipeline. */
export function PerformanceView() {
  const org = useOrgParam();
  const [groupBy, setGroupBy] = useState<PerfGroupBy>('agent');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const perf = usePerformance(org, {
    group_by: groupBy,
    from: startOfDay(from),
    to: endOfDay(to),
  });
  return (
    <div className="space-y-6">
      <PageHeader
        title="Execution performance"
        description="Latency percentiles and error rates from runs and steps."
      />
      <form
        aria-label="Filters"
        className="card grid gap-3 sm:grid-cols-3"
        onSubmit={(e) => e.preventDefault()}
      >
        <SelectField
          label="Group by"
          value={groupBy}
          onChange={(v) => setGroupBy(v as PerfGroupBy)}
          options={[
            { value: 'agent', label: 'Agent' },
            { value: 'model', label: 'Model' },
            { value: 'pipeline', label: 'Pipeline' },
          ]}
        />
        <TextField label="From date" type="date" value={from} onChange={setFrom} />
        <TextField label="To date" type="date" value={to} onChange={setTo} />
      </form>
      <QueryGate query={perf} noun="performance">
        {(rows) =>
          rows.length === 0 ? (
            <section className="card p-0">
              <EmptyState>No executions in this range.</EmptyState>
            </section>
          ) : (
            <section aria-label="Performance table" className="card overflow-x-auto p-0">
              <table className="w-full">
                <caption className="sr-only">Latency and error rate by {groupBy}</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {[
                      groupBy,
                      'Executions',
                      'Error rate',
                      'p50 latency',
                      'p95 latency',
                      'p99 latency',
                    ].map((h, i) => (
                      <th key={h} scope="col" className={`th ${i === 0 ? 'capitalize' : ''}`}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.map((r) => (
                    <tr key={r.key}>
                      <th scope="row" className="td text-left font-normal">
                        {r.label ?? r.key}
                      </th>
                      <td className="td">{formatCount(r.count)}</td>
                      <td className="td">
                        {formatPercent(r.error_rate, 1)}
                        {r.error_count !== undefined && (
                          <span className="block text-xs text-slate-600">
                            {formatCount(r.error_count)} errors
                          </span>
                        )}
                      </td>
                      <td className="td">{formatDuration(r.p50_ms)}</td>
                      <td className="td">{formatDuration(r.p95_ms)}</td>
                      <td className="td">{formatDuration(r.p99_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )
        }
      </QueryGate>
    </div>
  );
}
