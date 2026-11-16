'use client';

import Link from 'next/link';
import { endpoints } from '@/lib/api/endpoints';
import type { ConnectionTestResult } from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import {
  useAdapters,
  useConnection,
  useConnectionTests,
  useRefreshInt,
} from '@/lib/integrate-hooks';
import { isPlannedAdapter, PLANNED_EXPLANATION } from '@/lib/maturity';
import { ActionError, EmptyState, formatDate, PageHeader, QueryGate, useAction } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { StatusBadge } from './Maturity';

const CHECK_ORDER = ['config', 'egress', 'dns', 'tls', 'auth', 'capability'];

/** Per-check detail (config/egress/dns/tls/auth/capability) of one persisted test run. */
export function TestResultView({ result }: { result: ConnectionTestResult }) {
  const checks = [...result.checks].sort(
    (a, b) => CHECK_ORDER.indexOf(a.name) - CHECK_ORDER.indexOf(b.name),
  );
  return (
    <div className="space-y-2">
      <p className="text-sm">
        <StatusBadge status={result.outcome} />{' '}
        <span className="text-slate-600">
          {result.duration_ms} ms{result.created_at ? `, ${formatDate(result.created_at)}` : ''}
        </span>
        {result.error_class && (
          <>
            {' '}
            Error class: <code className="font-mono">{result.error_class}</code>
          </>
        )}
      </p>
      <table className="w-full">
        <caption className="sr-only">Checks for this test run</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {['Check', 'Result', 'Detail'].map((h) => (
              <th key={h} scope="col" className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {checks.map((c) => (
            <tr key={c.name}>
              <td className="td font-medium">{c.name}</td>
              <td className="td">
                <StatusBadge status={c.status} />
              </td>
              <td className="td">{c.detail ?? '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ConnectionTestsView({ id }: { id: string }) {
  const org = useOrgParam();
  const { can } = useCan(org);
  const connection = useConnection(org, id);
  const adapters = useAdapters();
  const tests = useConnectionTests(org, id);
  const refresh = useRefreshInt(org);
  const run = useAction();
  const adapter = adapters.data?.find((a) => a.key === connection.data?.adapter_key);
  const planned = adapters.isSuccess && connection.isSuccess && isPlannedAdapter(adapter);

  return (
    <div className="space-y-6">
      <p className="text-sm">
        <Link
          className="text-blue-800 underline"
          href={`/o/${encodeURIComponent(org)}/integrate/connections/${encodeURIComponent(id)}`}
        >
          Back to connection
        </Link>
      </p>
      <PageHeader
        title={`Test results${connection.data ? `: ${connection.data.name}` : ''}`}
        actions={
          can('connection:test') && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={planned || run.busy}
              onClick={async () => {
                const res = await run.run(() => endpoints.testConnection(org, id));
                if (res.ok) await refresh('connections');
              }}
            >
              {run.busy ? 'Testing...' : 'Run test'}
            </button>
          )
        }
      />
      {planned && (
        <p role="note" className="rounded-md border border-slate-300 bg-slate-100 p-3 text-sm">
          {PLANNED_EXPLANATION}
        </p>
      )}
      <ActionError error={run.error} title="Could not run the test" />
      <QueryGate query={tests} noun="test results">
        {(data) =>
          data.items.length === 0 ? (
            <section className="card p-0">
              <EmptyState>This connection has not been tested.</EmptyState>
            </section>
          ) : (
            <ol aria-label="Test history" className="space-y-4">
              {data.items.map((t) => (
                <li key={t.id} className="card">
                  <TestResultView result={t} />
                </li>
              ))}
            </ol>
          )
        }
      </QueryGate>
    </div>
  );
}
