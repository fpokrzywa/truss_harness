'use client';

import { useState } from 'react';
import type { Adapter } from '@/lib/api/integrate-types';
import { useAdapters, useConformanceRuns } from '@/lib/integrate-hooks';
import { adapterMaturity } from '@/lib/maturity';
import { EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { CapabilityTable, MaturityBadge, StatusBadge } from './Maturity';

export function AdaptersView() {
  const org = useOrgParam();
  const adapters = useAdapters();
  return (
    <div className="space-y-6">
      <PageHeader
        title="Adapters"
        description="Registered adapters and the evidence behind each capability. Maturity is derived from recorded conformance runs and cannot be set by an administrator."
      />
      <QueryGate query={adapters} noun="adapters">
        {(items) =>
          items.length === 0 ? (
            <section className="card p-0">
              <EmptyState>No adapters are registered.</EmptyState>
            </section>
          ) : (
            <div className="space-y-6">
              {items.map((a) => (
                <AdapterCard key={a.key} adapter={a} org={org} />
              ))}
            </div>
          )
        }
      </QueryGate>
    </div>
  );
}

function AdapterCard({ adapter, org }: { adapter: Adapter; org: string }) {
  const [showRuns, setShowRuns] = useState(false);
  const overall = adapterMaturity(adapter);
  return (
    <section id={adapter.key} aria-labelledby={`ad-${adapter.key}`} className="card space-y-3 p-0">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-4">
        <h2 id={`ad-${adapter.key}`} className="text-lg font-semibold">
          <span className="font-mono">{adapter.key}</span>{' '}
          <span className="text-sm font-normal text-slate-600">
            {adapter.kind}, version {adapter.version}
          </span>
        </h2>
        <MaturityBadge maturity={overall} />
      </div>
      {adapter.capabilities.length === 0 ? (
        <EmptyState>No capabilities declared.</EmptyState>
      ) : (
        <CapabilityTable
          rows={adapter.capabilities.map((c) => ({ ...c, adapter_key: adapter.key }))}
          caption={`Capabilities of ${adapter.key}`}
        />
      )}
      <div className="px-4 pb-4">
        <button
          type="button"
          className="btn"
          onClick={() => setShowRuns((s) => !s)}
          aria-expanded={showRuns}
        >
          {showRuns ? 'Hide' : 'Show'} conformance runs{' '}
          <span className="sr-only">for {adapter.key}</span>
        </button>
        {showRuns && <Runs adapterKey={adapter.key} org={org} />}
      </div>
    </section>
  );
}

function Runs({ adapterKey }: { adapterKey: string; org: string }) {
  const runs = useConformanceRuns(adapterKey);
  return (
    <div className="mt-3">
      <QueryGate query={runs} noun="conformance runs">
        {(items) =>
          items.length === 0 ? (
            <p className="text-sm text-slate-600">
              No conformance run has been recorded, so nothing here is validated.
            </p>
          ) : (
            <table className="w-full">
              <caption className="sr-only">Conformance runs for {adapterKey}</caption>
              <thead className="border-b border-slate-200">
                <tr>
                  {['Finished', 'Suite', 'Adapter version', 'Outcome'].map((h) => (
                    <th key={h} scope="col" className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {items.map((r) => (
                  <tr key={r.id}>
                    <td className="td">{formatDate(r.finished_at)}</td>
                    <td className="td">{r.suite_version}</td>
                    <td className="td">{r.adapter_version ?? '-'}</td>
                    <td className="td">
                      <StatusBadge status={r.outcome} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        }
      </QueryGate>
    </div>
  );
}
