'use client';

import Link from 'next/link';
import { useState } from 'react';
import { ApiError } from '@/lib/api/errors';
import { endpoints } from '@/lib/api/endpoints';
import type { Connection, ConnectionStatus, ConnectionTestResult } from '@/lib/api/integrate-types';
import { useCan } from '@/lib/hooks';
import {
  useAdapters,
  useConnection,
  useLatestDiscovery,
  useRefreshInt,
} from '@/lib/integrate-hooks';
import { normalizeDiscovery } from '@/lib/discovery';
import { connectionHref, connectionsHref } from '@/lib/scope';
import { adapterMaturity, isPlannedAdapter, PLANNED_EXPLANATION } from '@/lib/maturity';
import { useSecrets } from '@/lib/security-hooks';
import { ConfirmDialog } from '../ConfirmDialog';
import { ActionError, EmptyState, formatDate, PageHeader, QueryGate, useAction } from '../admin/ui';
import { useOrgParam } from '../security/useOrg';
import { ConnectionConsole } from './ConnectionConsole';
import { TestResultView } from './ConnectionTests';
import { MaturityBadge, StatusBadge } from './Maturity';
import { scopeLabel } from './ConnectionsView';

const TRANSITIONS: Record<ConnectionStatus, { to: ConnectionStatus; label: string }[]> = {
  draft: [
    { to: 'active', label: 'Activate' },
    { to: 'retired', label: 'Retire' },
  ],
  active: [
    { to: 'disabled', label: 'Disable' },
    { to: 'retired', label: 'Retire' },
  ],
  disabled: [
    { to: 'active', label: 'Re-enable' },
    { to: 'retired', label: 'Retire' },
  ],
  retired: [],
};

function renderValue(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v);
}

export function ConnectionDetailView({ id }: { id: string }) {
  const org = useOrgParam();
  const { can } = useCan(org);
  const connection = useConnection(org, id);
  const adapters = useAdapters();
  const secrets = useSecrets(org);
  const refresh = useRefreshInt(org);
  const transition = useAction();
  const test = useAction();
  const [pending, setPending] = useState<ConnectionStatus | null>(null);
  const [lastTest, setLastTest] = useState<ConnectionTestResult | null>(null);

  return (
    <div className="space-y-6">
      <p className="text-sm">
        <Link className="text-blue-800 underline" href={connectionsHref(org)}>
          Back to connections
        </Link>
      </p>
      <QueryGate query={connection} noun="connection">
        {(c: Connection) => {
          const adapter = adapters.data?.find((a) => a.key === c.adapter_key);
          const planned = adapters.isSuccess && isPlannedAdapter(adapter);
          const secretName = secrets.data?.items.find((s) => s.id === c.secret_id)?.name;
          const configEntries = Object.entries(c.config);
          const consoleBlocked = planned
            ? PLANNED_EXPLANATION
            : c.status !== 'active'
              ? `The connection is ${c.status}. Only an active connection can be invoked.`
              : null;

          async function apply(to: ConnectionStatus) {
            const res = await transition.run(() => endpoints.setConnectionStatus(org, c, to));
            setPending(null);
            if (res.ok) await refresh('connections');
          }

          return (
            <>
              <PageHeader
                title={c.name}
                actions={
                  <Link className="btn" href={connectionHref(org, id, 'tests')}>
                    Test history
                  </Link>
                }
              />
              <dl className="card grid gap-3 text-sm sm:grid-cols-2">
                <Item label="Status">
                  <StatusBadge status={c.status} />
                </Item>
                <Item label="Type">{c.connection_type}</Item>
                <Item label="Adapter">
                  <span className="font-mono">{c.adapter_key}</span>{' '}
                  {adapters.isSuccess && <MaturityBadge maturity={adapterMaturity(adapter)} />}
                </Item>
                <Item label="Scope">{scopeLabel(c)}</Item>
                <Item label="Access mode">
                  {c.access_mode === 'read_write' ? 'Read and write' : 'Read only'}
                </Item>
                <Item label="Authentication">
                  {c.auth_method}
                  {c.secret_id ? ` using secret ${secretName ?? c.secret_id}` : ''}
                </Item>
                <Item label="Egress policy">{c.egress_policy_id ?? 'Default policy'}</Item>
                <Item label="Last tested">{formatDate(c.last_tested_at)}</Item>
              </dl>

              <section aria-labelledby="cfg-h" className="card space-y-2">
                <h2 id="cfg-h" className="text-lg font-semibold">
                  Configuration
                </h2>
                {configEntries.length === 0 ? (
                  <p className="text-sm text-slate-600">No configuration values.</p>
                ) : (
                  <dl className="grid gap-2 text-sm sm:grid-cols-2">
                    {configEntries.map(([k, v]) => (
                      <Item key={k} label={k}>
                        <span className="font-mono break-all">{renderValue(v)}</span>
                      </Item>
                    ))}
                  </dl>
                )}
                <p className="text-xs text-slate-600">
                  Editing the configuration moves the connection back to draft until it is
                  re-tested.
                </p>
              </section>

              {planned && (
                <p
                  role="note"
                  className="rounded-md border border-slate-300 bg-slate-100 p-3 text-sm"
                >
                  {PLANNED_EXPLANATION} Testing, activation and invocation are unavailable.
                </p>
              )}

              {can('connection:manage') && (
                <section aria-labelledby="status-h" className="card space-y-3">
                  <h2 id="status-h" className="text-lg font-semibold">
                    Status
                  </h2>
                  <p className="text-sm text-slate-600">
                    Activation requires a passing test within the last 24 hours. Re-enabling a
                    disabled connection requires a fresh passing test.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {TRANSITIONS[c.status].map(({ to, label }) => (
                      <button
                        key={to}
                        type="button"
                        className="btn"
                        disabled={transition.busy || (to === 'active' && planned)}
                        onClick={() => (to === 'retired' ? setPending(to) : void apply(to))}
                      >
                        {label}
                      </button>
                    ))}
                    {c.status === 'retired' && (
                      <span className="text-sm text-slate-600">
                        Retired connections cannot change status.
                      </span>
                    )}
                  </div>
                  <ActionError error={transition.error} title="Could not change the status" />
                  <ConfirmDialog
                    open={pending !== null}
                    title="Retire connection?"
                    confirmLabel="Retire connection"
                    busy={transition.busy}
                    onConfirm={() => pending && void apply(pending)}
                    onCancel={() => setPending(null)}
                  >
                    <p>
                      <strong>{c.name}</strong> will stop working and cannot be reactivated.
                    </p>
                  </ConfirmDialog>
                </section>
              )}

              {can('connection:test') && (
                <section aria-labelledby="test-h" className="card space-y-3">
                  <h2 id="test-h" className="text-lg font-semibold">
                    Test
                  </h2>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={planned || test.busy}
                    onClick={async () => {
                      const res = await test.run(() => endpoints.testConnection(org, id));
                      if (res.ok) {
                        setLastTest(res.value);
                        await refresh('connections');
                      }
                    }}
                  >
                    {test.busy ? 'Testing...' : 'Test connection'}
                  </button>
                  <ActionError error={test.error} title="Could not run the test" />
                  {lastTest && <TestResultView result={lastTest} />}
                </section>
              )}

              <Discovery org={org} id={id} planned={planned} canTest={can('connection:test')} />

              {can('connection:use') && (
                <ConnectionConsole org={org} connection={c} blockedReason={consoleBlocked} />
              )}
            </>
          );
        }}
      </QueryGate>
    </div>
  );
}

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="font-medium text-slate-600">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Discovery({
  org,
  id,
  planned,
  canTest,
}: {
  org: string;
  id: string;
  planned: boolean;
  canTest: boolean;
}) {
  const latest = useLatestDiscovery(org, id);
  const refresh = useRefreshInt(org);
  const discover = useAction();
  const none = latest.isError && latest.error instanceof ApiError && latest.error.status === 404;
  const view = latest.data ? normalizeDiscovery(latest.data) : null;

  return (
    <section aria-labelledby="disc-h" className="card space-y-3">
      <h2 id="disc-h" className="text-lg font-semibold">
        Discovery
      </h2>
      {canTest && (
        <button
          type="button"
          className="btn"
          disabled={planned || discover.busy}
          onClick={async () => {
            const res = await discover.run(() => endpoints.discoverConnection(org, id));
            if (res.ok) await refresh('connections');
          }}
        >
          {discover.busy ? 'Discovering...' : 'Run discovery'}
        </button>
      )}
      <ActionError error={discover.error} title="Discovery failed" />
      {latest.isPending ? (
        <p role="status" className="text-sm text-slate-600">
          Loading discovery...
        </p>
      ) : none ? (
        <EmptyState>No discovery has been run for this connection.</EmptyState>
      ) : latest.isError ? (
        <ActionError error={latest.error} title="Could not load discovery" />
      ) : view ? (
        <div className="space-y-4">
          {(latest.data?.discovered_at ?? latest.data?.created_at) && (
            <p className="text-xs text-slate-600">
              Discovered{' '}
              {formatDate((latest.data.discovered_at ?? latest.data.created_at) as string)}
            </p>
          )}
          {view.paths.length === 0 && view.tools.length === 0 && view.tables.length === 0 && (
            <EmptyState>The last discovery found nothing.</EmptyState>
          )}
          {view.paths.length > 0 && (
            <div>
              <h3 className="font-medium">API paths ({view.paths.length})</h3>
              <ul className="mt-1 max-h-64 overflow-auto text-sm">
                {view.paths.map((p) => (
                  <li key={p.path} className="font-mono">
                    {p.methods.length > 0 && (
                      <span className="text-slate-600">{p.methods.join(', ')} </span>
                    )}
                    {p.path}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {view.tools.length > 0 && (
            <div>
              <h3 className="font-medium">MCP tools ({view.tools.length})</h3>
              <ul className="mt-1 text-sm">
                {view.tools.map((t) => (
                  <li key={t.name}>
                    <span className="font-mono">{t.name}</span>
                    {t.description && <span className="text-slate-600">: {t.description}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {view.tables.length > 0 && (
            <div>
              <h3 className="font-medium">Tables ({view.tables.length})</h3>
              <ul className="mt-1 space-y-2 text-sm">
                {view.tables.map((t) => (
                  <li key={`${t.schema ?? ''}.${t.name}`}>
                    <span className="font-mono">{t.schema ? `${t.schema}.${t.name}` : t.name}</span>
                    <span className="text-slate-600">
                      {' '}
                      {t.columns.map((c) => (c.type ? `${c.name} ${c.type}` : c.name)).join(', ')}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}
