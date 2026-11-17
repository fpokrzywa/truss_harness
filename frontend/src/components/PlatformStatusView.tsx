'use client';

import { useQuery } from '@tanstack/react-query';
import { endpoints } from '@/lib/api/endpoints';
import type { PlatformInstallation, PlatformStatus, Readiness } from '@/lib/api/types';
import { keys } from '@/lib/hooks';
import { installationKey } from './PlatformSettingsView';
import { ErrorMessage } from './ErrorMessage';
import { QueryError } from './QueryError';
import { TelemetryPanel } from './governance/TelemetryPanel';

/** "not_deployed" -> "Not deployed"; the raw reported value is shown alongside. */
export function humanizeStatus(raw: string): string {
  const s = raw.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function tone(raw: string): string {
  if (raw === 'ok' || raw === 'ready' || raw === 'healthy')
    return 'border-green-300 bg-green-50 text-green-800';
  if (raw === 'not_deployed' || raw === 'not_running')
    return 'border-slate-300 bg-slate-100 text-slate-700';
  if (raw === 'stale') return 'border-amber-300 bg-amber-50 text-amber-900';
  return 'border-red-300 bg-red-50 text-red-800';
}

function StatusBadge({ value }: { value: string }) {
  return (
    <span className={`badge ${tone(value)}`}>
      {humanizeStatus(value)} <code className="ml-1 font-mono font-normal">({value})</code>
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <dt className="text-sm text-slate-700">{label}</dt>
      <dd className="text-sm text-slate-900">{children}</dd>
    </div>
  );
}

export function PlatformStatusContent({
  status,
  ready,
  installation,
}: {
  status: PlatformStatus;
  ready: { httpStatus: number; body: Readiness | undefined };
  /** From the platform-admin installation endpoint; absent when not accessible. */
  installation?: PlatformInstallation;
}) {
  const checks = ready.body?.checks ?? {};
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900">Platform Status</h1>

      <section className="card" aria-labelledby="components-title">
        <h2 id="components-title" className="text-base font-semibold text-slate-900">
          Components
        </h2>
        <dl className="mt-2 divide-y divide-slate-100">
          <Row label="API readiness">
            <StatusBadge
              value={ready.body?.status ?? (ready.httpStatus === 503 ? 'not_ready' : 'unknown')}
            />
          </Row>
          {Object.entries(checks).map(([name, value]) => (
            <Row key={name} label={`Readiness check: ${name}`}>
              <StatusBadge value={value} />
            </Row>
          ))}
          <Row label="Database">
            <StatusBadge value={status.database.status} />
          </Row>
          <Row label="Migrations (current / head)">
            <code className="font-mono text-xs">
              {status.database.revision ?? 'none'} / {status.database.head ?? 'none'}
            </code>
          </Row>
          <Row label="Worker">
            <StatusBadge value={status.worker.status} />
          </Row>
          <Row label="Workers reporting">{status.worker.count ?? 0}</Row>
          <Row label="Last worker heartbeat">
            {status.worker.last_heartbeat_at ? (
              <time dateTime={status.worker.last_heartbeat_at}>
                {new Date(status.worker.last_heartbeat_at).toLocaleString()}
              </time>
            ) : (
              'Never'
            )}
          </Row>
        </dl>
      </section>

      <section className="card" aria-labelledby="install-title">
        <h2 id="install-title" className="text-base font-semibold text-slate-900">
          Installation
        </h2>
        <dl className="mt-2 divide-y divide-slate-100">
          <Row label="Version">{installation?.version ?? status.version}</Row>
          {installation && (
            <>
              <Row label="Migration revision">
                <code className="font-mono text-xs">
                  {installation.migration.current ?? 'none'}
                </code>{' '}
                <StatusBadge value={installation.migration.ok ? 'ok' : 'mismatch'} />
              </Row>
              <Row label="Deployment mode">{installation.deployment_mode}</Row>
            </>
          )}
          <Row label="Environment">{status.environment}</Row>
          <Row label="Local sign-in">{status.auth.local_enabled ? 'Enabled' : 'Disabled'}</Row>
          <Row label="OIDC providers configured">{status.auth.oidc_providers}</Row>
        </dl>
      </section>
    </div>
  );
}

export function PlatformStatusView() {
  const status = useQuery({
    queryKey: keys.platformStatus,
    queryFn: () => endpoints.platformStatus(),
  });
  const ready = useQuery({ queryKey: keys.ready, queryFn: () => endpoints.ready() });
  // Optional enrichment: a 403 (not a platform admin) or any failure just omits the extra rows.
  const installation = useQuery({
    queryKey: installationKey,
    queryFn: () => endpoints.platformInstallation(),
    retry: false,
  });

  if (status.isPending || ready.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading platform status...
      </p>
    );
  }
  if (status.isError) return <QueryError error={status.error} />;
  if (ready.isError) return <ErrorMessage error={ready.error} title="Could not read /api/ready" />;
  return (
    <div className="space-y-6">
      <PlatformStatusContent
        status={status.data}
        ready={{ httpStatus: ready.data.status, body: ready.data.body }}
        installation={installation.data}
      />
      <TelemetryPanel />
    </div>
  );
}
