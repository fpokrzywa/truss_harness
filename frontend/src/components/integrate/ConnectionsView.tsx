'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import type { Connection } from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import { useAdapters, useConnections } from '@/lib/integrate-hooks';
import { adapterMaturity } from '@/lib/maturity';
import { connectionHref } from '@/lib/scope';
import { EmptyState, formatDate, PageHeader, QueryGate, SelectField } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { ConnectionWizard, CONNECTION_TYPES } from './ConnectionWizard';
import { MaturityBadge, StatusBadge } from './Maturity';
import { SlugScopeSelect } from './SlugScopeSelect';

export function scopeLabel(c: Connection): string {
  if (c.scope?.project) return `Project ${c.scope.project}`;
  if (c.scope?.workspace) return `Workspace ${c.scope.workspace}`;
  return 'Organization';
}

export function ConnectionsView() {
  const org = useOrgParam();
  const router = useRouter();
  const { can } = useCan(org);
  const adapters = useAdapters();
  const [type, setType] = useState('');
  const [status, setStatus] = useState('');
  const [scope, setScope] = useState({ workspace: '', project: '' });
  const [creating, setCreating] = useState(false);
  const connections = useConnections(org, {
    type: type || undefined,
    status: status || undefined,
    workspace: scope.workspace || undefined,
    project: scope.project || undefined,
  });

  return (
    <div className="space-y-6">
      <PageHeader
        title="Connections"
        description="Governed connections to REST APIs, MCP servers, databases and model endpoints. Credentials are referenced by secret, never stored here."
        actions={
          can('connection:manage') && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Create connection
            </button>
          )
        }
      />
      <div className="flex flex-wrap items-end gap-3">
        <SelectField
          label="Type"
          value={type}
          onChange={setType}
          options={[{ value: '', label: 'All types' }, ...CONNECTION_TYPES]}
        />
        <SelectField
          label="Status"
          value={status}
          onChange={setStatus}
          options={[
            { value: '', label: 'All statuses' },
            ...['draft', 'active', 'disabled', 'retired'].map((s) => ({ value: s, label: s })),
          ]}
        />
        <SlugScopeSelect org={org} {...scope} onChange={setScope} idPrefix="Filter " />
      </div>
      <QueryGate query={connections} noun="connections">
        {(data) => (
          <section aria-label="Connections" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No connections match.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Connections</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {['Name', 'Type', 'Adapter', 'Scope', 'Status', 'Health', 'Last tested'].map(
                      (h) => (
                        <th key={h} scope="col" className="th">
                          {h}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((c) => (
                    <tr key={c.id}>
                      <td className="td font-medium">
                        <Link className="text-blue-800 underline" href={connectionHref(org, c.id)}>
                          {c.name}
                        </Link>
                      </td>
                      <td className="td">{c.connection_type}</td>
                      <td className="td">
                        <span className="font-mono">{c.adapter_key}</span>{' '}
                        {adapters.data && (
                          <MaturityBadge
                            maturity={adapterMaturity(
                              adapters.data.find((a) => a.key === c.adapter_key),
                            )}
                          />
                        )}
                      </td>
                      <td className="td">{scopeLabel(c)}</td>
                      <td className="td">
                        <StatusBadge status={c.status} />
                      </td>
                      <td className="td">{c.health ? <StatusBadge status={c.health} /> : '-'}</td>
                      <td className="td">{formatDate(c.last_tested_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </QueryGate>
      {creating && (
        <ConnectionWizard
          org={org}
          onClose={() => setCreating(false)}
          onCreated={(id) => router.push(connectionHref(org, id))}
        />
      )}
    </div>
  );
}
