'use client';

import type { UseQueryResult } from '@tanstack/react-query';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useId, useState, type ReactNode } from 'react';
import type { TargetOut } from '@/lib/api/delivery-types';
import type { Adapter, Connection, ModelProvider } from '@/lib/api/integrate-types';
import { useTargets } from '@/lib/delivery-hooks';
import { usePermissions } from '@/lib/hooks';
import { useAdapters, useConnections, useProviders } from '@/lib/integrate-hooks';
import { adapterMaturity } from '@/lib/maturity';
import { connectionHref, deliverHref, integrateHref } from '@/lib/scope';
import { Tabs, type TabItem } from '../command/Tabs';
import { NoAccess } from '../NoAccess';
import { QueryError } from '../QueryError';
import { Badge, EmptyState, formatDate, PageHeader, QueryGate } from '../admin/ui';
import { useProjectQuery } from '../releases/ProjectScope';
import { useOrgParam } from '../security/useOrg';
import { ConnectionWizard } from './ConnectionWizard';
import { scopeLabel } from './ConnectionsView';
import { MaturityBadge, StatusBadge } from './Maturity';
import { SlugScopeSelect } from './SlugScopeSelect';

const DATA = 'Data and tools';
const MODELS = 'Model providers';
const TARGETS = 'Delivery targets';
const COLUMNS = ['Name', 'Kind', 'Scope', 'Status', 'Last test', 'Credential'];

interface Row {
  id: string;
  name: string;
  href: string;
  notes: string[];
  kind: ReactNode;
  scope: ReactNode;
  status: ReactNode;
  lastTest: ReactNode;
  credential: string;
}

/** Inherited from a wider scope than the chosen project (design 9, "Shared" tag). */
function Shared({ workspace }: { workspace: string | null | undefined }) {
  return (
    <>
      <Badge>Shared</Badge> {workspace ? `Shared: workspace ${workspace}` : 'Shared: client-wide'}
    </>
  );
}

function AdapterKind({ adapterKey, adapters }: { adapterKey: string; adapters?: Adapter[] }) {
  const adapter = adapters?.find((a) => a.key === adapterKey);
  return (
    <>
      {adapter?.display_name || adapterKey}{' '}
      {adapters && <MaturityBadge maturity={adapterMaturity(adapter)} />}
    </>
  );
}

/** A Databricks connection and a delivery target pointing at the same workspace host. */
function sameWorkspace(c: Connection, t: TargetOut): boolean {
  return c.adapter_key === 'databricks' && typeof c.config?.host === 'string'
    ? c.config.host === t.address
    : false;
}

function targetScope(t: TargetOut): string {
  if (t.project) return `Project ${t.project}`;
  if (t.workspace) return `Workspace ${t.workspace}`;
  return 'Organization';
}

function Section<T>({
  title,
  empty,
  query,
  rows,
}: {
  title: string;
  empty: string;
  query: UseQueryResult<T>;
  rows: (data: T) => Row[];
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="space-y-2">
      <h2 id={id} className="text-base font-semibold text-slate-900">
        {title}
      </h2>
      <QueryGate query={query} noun={title.toLowerCase()}>
        {(data) => {
          const items = rows(data);
          if (items.length === 0)
            return (
              <div className="card p-0">
                <EmptyState>{empty}</EmptyState>
              </div>
            );
          return (
            <div className="card p-0">
              <table className="w-full">
                <caption className="sr-only">{title}</caption>
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
                  {items.map((r) => (
                    <tr key={r.id}>
                      <td className="td font-medium">
                        <Link className="text-blue-800 underline" href={r.href}>
                          {r.name}
                        </Link>
                        {r.notes.map((n) => (
                          <p key={n} className="text-xs font-normal text-slate-600">
                            {n}
                          </p>
                        ))}
                      </td>
                      <td className="td">{r.kind}</td>
                      <td className="td">{r.scope}</td>
                      <td className="td">{r.status}</td>
                      <td className="td">{r.lastTest}</td>
                      <td className="td">{r.credential}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }}
      </QueryGate>
    </section>
  );
}

/** The Connections space (CM-6): connections, model providers and delivery targets of a scope. */
export function ConnectionsSpaceView() {
  const org = useOrgParam();
  const router = useRouter();
  const pathname = usePathname();
  const { ws, proj } = useProjectQuery();
  const [creating, setCreating] = useState(false);
  const permissions = usePermissions(org, ws || undefined, proj || undefined);
  const can = (key: string) => permissions.data?.has(key) ?? false;
  const canData = can('connection:read');
  const canModels = can('model:read');
  const canTargets = can('delivery:target:read');
  const adapters = useAdapters();
  const connections = useConnections(
    org,
    {
      workspace: ws || undefined,
      project: proj || undefined,
      include_shared: proj ? 'true' : undefined,
    },
    canData,
  );
  const providers = useProviders(org, canModels);
  const targets = useTargets(org, canTargets);

  function setScope(next: { workspace: string; project: string }) {
    const q = new URLSearchParams();
    if (next.workspace) q.set('ws', next.workspace);
    if (next.project) q.set('proj', next.project);
    const query = q.toString();
    router.replace(query ? `${pathname}?${query}` : pathname);
  }

  if (permissions.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading permissions...
      </p>
    );
  }
  if (permissions.isError) return <QueryError error={permissions.error} />;
  if (!canData && !canModels && !canTargets) return <NoAccess />;

  const inScope = (t: TargetOut) =>
    !ws ? true : t.workspace === ws && (!proj || t.project === proj || t.project === null);
  // Only rows already loaded are compared: a hidden or pending list matches nothing.
  const loadedConnections = canData ? (connections.data?.items ?? []) : [];
  const loadedTargets = canTargets ? (targets.data?.items ?? []).filter(inScope) : [];

  const connectionRows = (data: { items: Connection[] }): Row[] =>
    data.items.map((c) => ({
      id: c.id,
      name: c.name,
      href: connectionHref(org, c.id),
      notes: loadedTargets
        .filter((t) => sameWorkspace(c, t))
        .map((t) => `Same workspace as target ${t.name}`),
      kind: <AdapterKind adapterKey={c.adapter_key} adapters={adapters.data} />,
      scope: proj && !c.scope?.project ? <Shared workspace={c.scope?.workspace} /> : scopeLabel(c),
      status: <StatusBadge status={c.status} />,
      lastTest: c.last_tested_at ? (
        <>
          {formatDate(c.last_tested_at)}
          {c.last_test_outcome && (
            <>
              {' '}
              <StatusBadge status={c.last_test_outcome} />
            </>
          )}
        </>
      ) : (
        'never'
      ),
      credential: c.secret_id ? 'set' : 'not set',
    }));

  const providerRows = (data: { items: ModelProvider[] }): Row[] =>
    data.items.map((p) => {
      const connection = loadedConnections.find((c) => c.id === p.connection_id);
      return {
        id: p.id,
        name: p.name,
        href: `${integrateHref(org, 'providers')}/${encodeURIComponent(p.id)}`,
        notes: [],
        kind: <AdapterKind adapterKey={p.adapter_key} adapters={adapters.data} />,
        scope: 'Shared: client-wide',
        status: <StatusBadge status={p.health_status} />,
        lastTest: p.health_checked_at ? formatDate(p.health_checked_at) : 'never',
        credential: connection ? (connection.secret_id ? 'set' : 'not set') : '-',
      };
    });

  const targetRows = (data: { items: TargetOut[] }): Row[] =>
    data.items.filter(inScope).map((t) => ({
      id: t.id,
      name: t.name,
      href: deliverHref(org, 'targets'),
      notes: loadedConnections
        .filter((c) => sameWorkspace(c, t))
        .map((c) => `Same workspace as connection ${c.name}`),
      kind: t.platform_kind,
      scope: proj && t.project === null ? <Shared workspace={t.workspace} /> : targetScope(t),
      status: <StatusBadge status={t.status} />,
      lastTest: '-',
      credential: 'held by executor',
    }));

  const sections: TabItem[] = [];
  if (canData)
    sections.push({
      id: 'data',
      label: DATA,
      content: (
        <Section
          title={DATA}
          empty="No data and tools connections in this scope."
          query={connections}
          rows={connectionRows}
        />
      ),
    });
  if (canModels)
    sections.push({
      id: 'models',
      label: MODELS,
      content: (
        <Section
          title={MODELS}
          empty="No model providers in this scope."
          query={providers}
          rows={providerRows}
        />
      ),
    });
  if (canTargets)
    sections.push({
      id: 'targets',
      label: TARGETS,
      content: (
        <Section
          title={TARGETS}
          empty="No delivery targets in this scope."
          query={targets}
          rows={targetRows}
        />
      ),
    });
  const tabs: TabItem[] = [
    {
      id: 'all',
      label: 'All',
      content: (
        <div className="space-y-6">
          {sections.map((s) => (
            <div key={s.id}>{s.content}</div>
          ))}
        </div>
      ),
    },
    ...sections,
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Connections"
        description="Connections, model providers and delivery targets available in the chosen scope."
        actions={
          can('connection:manage') && (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Create connection
            </button>
          )
        }
      />
      <SlugScopeSelect org={org} workspace={ws} project={proj} onChange={setScope} />
      <Tabs label="Connection groups" tabs={tabs} />
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
