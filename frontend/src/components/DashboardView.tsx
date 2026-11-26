'use client';

import { useQuery } from '@tanstack/react-query';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { useDashboard, usePermissions } from '@/lib/hooks';
import { endpoints } from '@/lib/api/endpoints';
import { connectionsHref, projHref, settingsHref } from '@/lib/scope';
import type { Dashboard } from '@/lib/api/types';
import { ErrorMessage } from './ErrorMessage';
import { QueryError } from './QueryError';
import { formatDate } from './admin/ui';

const COUNT_LABELS: [keyof Dashboard['counts'], string][] = [
  ['workspaces', 'Workspaces'],
  ['projects', 'Projects'],
  ['members', 'Members'],
];

export function DashboardContent({ data, heading }: { data: Dashboard; heading: string }) {
  const counts = COUNT_LABELS.filter(([k]) => typeof data.counts[k] === 'number');
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900">{heading}</h1>

      <section aria-labelledby="counts-title">
        <h2 id="counts-title" className="sr-only">
          Resource counts
        </h2>
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {counts.map(([key, label]) => (
            <div key={key} className="card">
              <dt className="text-sm text-slate-600">{label}</dt>
              <dd className="mt-1 text-3xl font-semibold text-slate-900">{data.counts[key]}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section aria-labelledby="activity-title" className="card">
        <h2 id="activity-title" className="text-base font-semibold text-slate-900">
          Recent activity
        </h2>
        {data.recent_activity.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600">No recent activity in this scope.</p>
        ) : (
          <ul className="mt-2 divide-y divide-slate-100">
            {data.recent_activity.map((a, i) => (
              <li
                key={`${a.at}-${i}`}
                className="grid grid-cols-1 gap-x-3 gap-y-0.5 py-2 text-sm sm:grid-cols-[11rem_1fr]"
              >
                <time dateTime={a.at} className="text-slate-500">
                  {new Date(a.at).toLocaleString()}
                </time>
                <span className="min-w-0 break-words">
                  <span className="font-medium">{a.actor ?? 'System'}</span> {a.action}
                  {a.target ? <span className="text-slate-600"> {a.target}</span> : null}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/** The organization's MCP access at a glance; the switch stays on the settings screen. */
function McpAccessCard({ org }: { org: string }) {
  const settings = useQuery({
    queryKey: ['mcp-settings', org],
    queryFn: () => endpoints.getMcpSettings(org),
  });
  return (
    <section aria-labelledby="mcp-title" className="card mt-6 space-y-2">
      <h2 id="mcp-title" className="text-base font-semibold text-slate-900">
        MCP access
      </h2>
      {settings.isPending && (
        <p role="status" className="text-sm text-slate-600">
          Loading MCP settings...
        </p>
      )}
      {settings.isError && (
        <ErrorMessage error={settings.error} title="Could not load the MCP settings" />
      )}
      {settings.isSuccess && (
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[10rem_1fr]">
          <dt className="text-slate-600">Status</dt>
          <dd>{settings.data.enabled ? 'On' : 'Off'}</dd>
          {settings.data.updated_at && (
            <>
              <dt className="text-slate-600">Last changed</dt>
              <dd>{formatDate(settings.data.updated_at)}</dd>
            </>
          )}
          {settings.data.updated_by && (
            <>
              <dt className="text-slate-600">Changed by</dt>
              <dd className="font-mono text-xs">{settings.data.updated_by}</dd>
            </>
          )}
          <dt className="text-slate-600">Endpoint</dt>
          <dd className="font-mono text-xs break-all">
            {settings.data.endpoint_url ?? 'Not configured on this installation'}
          </dd>
        </dl>
      )}
      <p className="text-sm">
        <Link className="text-blue-800 underline" href={settingsHref(org, 'mcp')}>
          MCP settings
        </Link>
      </p>
    </section>
  );
}

/** Scope dashboard for /o/[org], /o/[org]/w/[ws], /o/[org]/w/[ws]/p/[proj] (FR-UI-002). */
export function DashboardView() {
  const params = useParams<{ org: string; ws?: string; proj?: string }>();
  const org = decodeURIComponent(params.org);
  const ws = params.ws ? decodeURIComponent(params.ws) : undefined;
  const proj = params.proj ? decodeURIComponent(params.proj) : undefined;
  const query = useDashboard(org, ws, proj);
  const permissions = usePermissions(org, ws, proj);
  const canRun = !!ws && !!proj && !!permissions.data?.has('agent:execute');
  const canConnections =
    !!ws &&
    !!proj &&
    ['connection:read', 'model:read', 'delivery:target:read'].some((p) => permissions.data?.has(p));
  const canMcp = !ws && !!permissions.data?.has('mcp:settings:read');

  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading command center...
      </p>
    );
  }
  if (query.isError) return <QueryError error={query.error} />;
  const heading = proj ? `Project: ${proj}` : ws ? `Workspace: ${ws}` : `Organization: ${org}`;
  return (
    <>
      {ws && proj && (canRun || canConnections) && (
        <p className="mb-4 flex flex-wrap gap-2">
          {canRun && (
            <Link className="btn btn-primary" href={`${projHref(org, ws, proj)}/run`}>
              Run an agent
            </Link>
          )}
          {canConnections && (
            <Link className="btn btn-primary" href={connectionsHref(org, ws, proj)}>
              Connections
            </Link>
          )}
        </p>
      )}
      <DashboardContent data={query.data} heading={heading} />
      {canMcp && <McpAccessCard org={org} />}
    </>
  );
}
