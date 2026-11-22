'use client';

import Link from 'next/link';
import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { DiscoveredModel, HealthResult } from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import { useAdapters, useProvider, useRefreshInt } from '@/lib/integrate-hooks';
import { isPlannedAdapter, PLANNED_EXPLANATION } from '@/lib/maturity';
import { connectionHref } from '@/lib/scope';
import { ActionError, EmptyState, formatDate, PageHeader, QueryGate, useAction } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { StatusBadge } from './Maturity';

/** Query string that pre-fills the register-model dialog on the Models page. */
export function registerHref(org: string, providerId: string, m: DiscoveredModel): string {
  const q = new URLSearchParams({ register: '1', provider_id: providerId, model_key: m.model_key });
  if (m.display_name) q.set('display_name', m.display_name);
  if (m.context_window !== undefined) q.set('context_window', String(m.context_window));
  if (m.max_output_tokens !== undefined) q.set('max_output_tokens', String(m.max_output_tokens));
  if (m.capabilities?.length) q.set('capabilities', m.capabilities.join(','));
  return `/o/${encodeURIComponent(org)}/integrate/models?${q.toString()}`;
}

export function ProviderDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const { can } = useCan(org);
  const provider = useProvider(org, id);
  const adapters = useAdapters();
  const refresh = useRefreshInt(org);
  const health = useAction();
  const discover = useAction();
  const [result, setResult] = useState<HealthResult | null>(null);
  const [found, setFound] = useState<DiscoveredModel[] | null>(null);
  const canManage = can('model:manage');

  return (
    <div className="space-y-6">
      <p className="text-sm">
        <Link
          className="text-blue-800 underline"
          href={`/o/${encodeURIComponent(org)}/integrate/providers`}
        >
          Back to providers
        </Link>
      </p>
      <QueryGate query={provider} noun="provider">
        {(p) => {
          const adapter = adapters.data?.find((a) => a.key === p.adapter_key);
          const planned = adapters.isSuccess && isPlannedAdapter(adapter);
          return (
            <>
              <PageHeader title={p.name} />
              <dl className="card grid gap-3 text-sm sm:grid-cols-2">
                <div>
                  <dt className="font-medium text-slate-600">Adapter</dt>
                  <dd className="font-mono">{p.adapter_key}</dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-600">Connection</dt>
                  <dd>
                    <Link
                      className="text-blue-800 underline"
                      href={connectionHref(org, p.connection_id)}
                    >
                      View connection
                    </Link>
                  </dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-600">Last known health</dt>
                  <dd>
                    <StatusBadge status={p.health_status ?? 'unknown'} />{' '}
                    {formatDate(p.health_checked_at)}
                  </dd>
                </div>
              </dl>

              {planned && (
                <p
                  role="note"
                  className="rounded-md border border-slate-300 bg-slate-100 p-3 text-sm"
                >
                  {PLANNED_EXPLANATION}
                </p>
              )}

              <section aria-labelledby="health-h" className="card space-y-3">
                <h2 id="health-h" className="text-lg font-semibold">
                  Health check
                </h2>
                {canManage && (
                  <button
                    type="button"
                    className="btn"
                    disabled={planned || health.busy}
                    onClick={async () => {
                      const res = await health.run(() => endpoints.healthCheck(org, id));
                      if (res.ok) {
                        setResult(res.value);
                        await refresh('providers');
                      }
                    }}
                  >
                    {health.busy ? 'Checking...' : 'Run health check'}
                  </button>
                )}
                <ActionError error={health.error} title="Health check failed" />
                {result && (
                  <p role="status" className="text-sm">
                    <StatusBadge status={result.status} /> {result.latency_ms} ms, checked{' '}
                    {formatDate(result.checked_at)}
                    {result.detail ? `. ${result.detail}` : ''}
                  </p>
                )}
              </section>

              <section aria-labelledby="disc-h" className="card space-y-3">
                <h2 id="disc-h" className="text-lg font-semibold">
                  Discover models
                </h2>
                <p className="text-sm text-slate-600">
                  Lists models the provider reports. Nothing is registered automatically.
                </p>
                {canManage && (
                  <button
                    type="button"
                    className="btn"
                    disabled={planned || discover.busy}
                    onClick={async () => {
                      const res = await discover.run(() => endpoints.discoverModels(org, id));
                      if (res.ok) setFound(res.value);
                    }}
                  >
                    {discover.busy ? 'Discovering...' : 'Discover models'}
                  </button>
                )}
                <ActionError error={discover.error} title="Discovery failed" />
                {found &&
                  (found.length === 0 ? (
                    <EmptyState>The provider reported no models.</EmptyState>
                  ) : (
                    <table className="w-full">
                      <caption className="sr-only">Discovered models</caption>
                      <thead className="border-b border-slate-200">
                        <tr>
                          <th scope="col" className="th">
                            Model
                          </th>
                          <th scope="col" className="th">
                            <span className="sr-only">Actions</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {found.map((m) => (
                          <tr key={m.model_key}>
                            <td className="td font-mono">{m.model_key}</td>
                            <td className="td text-right">
                              {can('model:manage') && (
                                <Link className="btn" href={registerHref(org, id, m)}>
                                  Register <span className="sr-only">{m.model_key}</span>
                                </Link>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ))}
              </section>
            </>
          );
        }}
      </QueryGate>
    </div>
  );
}
