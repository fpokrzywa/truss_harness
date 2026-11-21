'use client';

import Link from 'next/link';
import type { EvaluationRunOut } from '@/lib/api/evaluation-types';
import { useEvaluationRun } from '@/lib/release-hooks';
import { orgHref, releaseHref } from '@/lib/scope';
import { Badge, EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { StatusBadge } from '../integrate/Maturity';
import { useOrgParam } from '../security/useOrg';

/** One evaluation run: metrics against the release's thresholds and per-case scores. */
export function EvaluationRunView({ id }: { id: string }) {
  const org = useOrgParam();
  const run = useEvaluationRun(org, id);
  return (
    <div className="space-y-6">
      <QueryGate query={run} noun="evaluation">
        {(r) => <RunBody org={org} run={r} />}
      </QueryGate>
    </div>
  );
}

interface Metric {
  label: string;
  value: string | number | null;
  threshold: string | number | undefined;
  /** True when the measured value meets the threshold. */
  meets: (value: number, threshold: number) => boolean;
}

function RunBody({ org, run }: { org: string; run: EvaluationRunOut }) {
  const pending = run.status === 'queued' || run.status === 'running';
  const metrics: Metric[] = [
    {
      label: 'Accuracy',
      value: run.accuracy,
      threshold: run.thresholds.min_accuracy,
      meets: (v, t) => v >= t,
    },
    {
      label: 'p95 latency (ms)',
      value: run.p95_latency_ms,
      threshold: run.thresholds.max_p95_latency_ms,
      meets: (v, t) => v <= t,
    },
    {
      label: 'Total cost (USD)',
      value: run.total_cost_usd,
      threshold: run.thresholds.max_total_cost_usd,
      meets: (v, t) => v <= t,
    },
  ];

  return (
    <>
      <PageHeader title="Evaluation run" description="An evaluation of a sealed release" />
      <section aria-label="Evaluation" className="card">
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[12rem_1fr]">
          <dt className="text-slate-600">Status</dt>
          <dd>
            <StatusBadge status={run.status} />
            {run.status_reason && <span className="ml-2">{run.status_reason}</span>}
          </dd>
          <dt className="text-slate-600">Release</dt>
          <dd>
            <Link
              className="font-mono text-xs text-blue-800 underline break-all"
              href={releaseHref(org, run.release_id)}
            >
              {run.release_digest}
            </Link>
          </dd>
          <dt className="text-slate-600">Dataset digest</dt>
          <dd className="font-mono text-xs break-all">{run.dataset_digest}</dd>
          <dt className="text-slate-600">Started</dt>
          <dd>{formatDate(run.started_at)}</dd>
          <dt className="text-slate-600">Finished</dt>
          <dd>{formatDate(run.finished_at)}</dd>
        </dl>
        {pending && (
          <p role="status" className="mt-3 text-sm text-slate-700">
            The evaluation is {run.status}; results are pending and refresh automatically.
          </p>
        )}
      </section>

      <section aria-label="Metrics" className="card overflow-x-auto p-0">
        <table className="w-full">
          <caption className="px-4 pt-4 text-left text-base font-semibold">Metrics</caption>
          <thead className="border-b border-slate-200">
            <tr>
              {['Metric', 'Measured', 'Threshold', 'Result'].map((h) => (
                <th key={h} scope="col" className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {metrics.map((m) => (
              <tr key={m.label}>
                <th scope="row" className="td text-left font-medium">
                  {m.label}
                </th>
                <td className="td">{m.value ?? 'pending'}</td>
                <td className="td">{m.threshold ?? '-'}</td>
                <td className="td">
                  <Verdict metric={m} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section aria-label="Cases" className="card overflow-x-auto p-0">
        <h2 className="px-4 pt-4 text-base font-semibold">Cases</h2>
        {!run.results ? (
          <EmptyState>Case results are pending.</EmptyState>
        ) : run.results.length === 0 ? (
          <EmptyState>No case results.</EmptyState>
        ) : (
          <table className="w-full">
            <caption className="sr-only">Case results</caption>
            <thead className="border-b border-slate-200">
              <tr>
                {['Case', 'Result', 'Latency (ms)', 'Cost (USD)', 'Run', 'Failed assertions'].map(
                  (h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {run.results.map((c) => {
                const failed = c.assertions.filter((a) => !a.passed);
                return (
                  <tr key={c.case_id}>
                    <td className="td font-mono text-xs">{c.case_id}</td>
                    <td className="td">
                      {c.passed ? (
                        <Badge tone="good">passed</Badge>
                      ) : (
                        <Badge tone="bad">failed</Badge>
                      )}
                    </td>
                    <td className="td">{c.latency_ms}</td>
                    <td className="td">{c.cost_usd}</td>
                    <td className="td">
                      <Link
                        className="text-blue-800 underline"
                        href={`${orgHref(org)}/runs/${encodeURIComponent(c.run_id)}`}
                      >
                        Open run <span className="sr-only">of case {c.case_id}</span>
                      </Link>
                    </td>
                    <td className="td">
                      {failed.length === 0 ? (
                        '-'
                      ) : (
                        <ul>
                          {failed.map((a) => (
                            <li key={a.id}>
                              <span className="font-mono text-xs">{a.id}</span>:{' '}
                              {a.reason ?? 'failed'}
                            </li>
                          ))}
                        </ul>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

function Verdict({ metric }: { metric: Metric }) {
  if (metric.value === null) return <>pending</>;
  if (metric.threshold === undefined) return <>-</>;
  return metric.meets(Number(metric.value), Number(metric.threshold)) ? (
    <Badge tone="good">meets</Badge>
  ) : (
    <Badge tone="bad">misses</Badge>
  );
}
