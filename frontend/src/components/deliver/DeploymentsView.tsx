'use client';

import Link from 'next/link';
import { useDeployments, useTargets } from '@/lib/delivery-hooks';
import { deliverHref } from '@/lib/scope';
import { EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { DeploymentStatusBadge } from './DeploymentStatusBadge';
import { targetName } from './targetName';

const COLUMNS = ['Application key', 'Target', 'Status', 'Status reason', 'Created', 'Updated'];

/** Deployments of approved plans. Server text renders as plain React text only. */
export function DeploymentsView() {
  const org = useOrgParam();
  const deployments = useDeployments(org);
  const targets = useTargets(org);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deployments"
        description="Approved plans applied by an executor. An unknown outcome needs an operator to reconcile it or mark it failed."
      />
      <QueryGate query={deployments} noun="deployments">
        {(data) => (
          <div className="card overflow-x-auto p-0">
            {data.items.length === 0 ? (
              <EmptyState>No deployments yet.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Deployments</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {COLUMNS.map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((d) => (
                    <tr key={d.id}>
                      <td className="td font-mono text-xs">
                        <Link
                          className="text-blue-800 underline"
                          href={deliverHref(org, `deployments/${encodeURIComponent(d.id)}`)}
                        >
                          {d.application_key}
                        </Link>
                      </td>
                      <td className="td">{targetName(targets.data?.items, d.target_id)}</td>
                      <td className="td">
                        <DeploymentStatusBadge status={d.status} />
                      </td>
                      <td className="td">{d.status_reason ?? '-'}</td>
                      <td className="td">{formatDate(d.created_at)}</td>
                      <td className="td">{formatDate(d.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </QueryGate>
    </div>
  );
}
