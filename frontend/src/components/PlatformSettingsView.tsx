'use client';

import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import type { PlatformInstallation } from '@/lib/api/types';
import { QueryError } from './QueryError';

export const installationKey = ['platform-installation'] as const;

const NOT_REPORTED = <span className="text-slate-600 italic">not reported</span>;

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <dt className="text-sm text-slate-700">{label}</dt>
      <dd className="text-right text-sm text-slate-900">{children}</dd>
    </div>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="card" aria-labelledby={id}>
      <h2 id={id} className="text-base font-semibold text-slate-900">
        {title}
      </h2>
      <dl className="mt-2 divide-y divide-slate-100">{children}</dl>
    </section>
  );
}

function val(v: number | string | undefined, unit = ''): ReactNode {
  return v === undefined || v === null ? NOT_REPORTED : `${v}${unit}`;
}

function yesNo(v: boolean | undefined): ReactNode {
  return v === undefined ? NOT_REPORTED : v ? 'Yes' : 'No';
}

export function PlatformSettingsContent({ data }: { data: PlatformInstallation }) {
  const flags = Object.entries(data.feature_flags ?? {});
  const oidc = data.auth?.oidc_providers ?? [];
  const m = data.migration;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900">Platform Settings</h1>
      <Section id="set-install" title="Installation">
        <Row label="Version">{data.version}</Row>
        <Row label="Environment">{data.env}</Row>
        <Row label="Deployment mode">{data.deployment_mode}</Row>
        <Row label="Migration revision (current)">
          <code className="font-mono text-xs">{m.current ?? 'none'}</code>
        </Row>
        <Row label="Migration revision (expected)">
          <code className="font-mono text-xs">{m.expected_head ?? 'none'}</code>
        </Row>
        <Row label="Migrations up to date">
          <span
            className={`badge ${m.ok ? 'border-green-300 bg-green-50 text-green-800' : 'border-red-300 bg-red-50 text-red-800'}`}
          >
            {m.ok ? 'OK' : 'Mismatch'}
          </span>
        </Row>
      </Section>

      <Section id="set-flags" title="Feature flags">
        {flags.length === 0 ? (
          <Row label="Flags">{NOT_REPORTED}</Row>
        ) : (
          flags.map(([k, v]) => (
            <Row key={k} label={k}>
              {v ? 'Enabled' : 'Disabled'}
            </Row>
          ))
        )}
      </Section>

      <Section id="set-auth" title="Authentication">
        <Row label="Local sign-in">
          {data.auth?.local_enabled === undefined
            ? NOT_REPORTED
            : data.auth.local_enabled
              ? 'Enabled'
              : 'Disabled'}
        </Row>
        {oidc.length === 0 ? (
          <Row label="OIDC providers">None configured</Row>
        ) : (
          oidc.map((p) => (
            <Row key={`${p.name}|${p.issuer_host}`} label={`OIDC provider: ${p.name}`}>
              <code className="font-mono text-xs">{p.issuer_host}</code>
            </Row>
          ))
        )}
      </Section>

      <Section id="set-security" title="Security">
        <Row label="Session idle timeout">{val(data.security?.session_idle_minutes, ' min')}</Row>
        <Row label="Session absolute lifetime">
          {val(data.security?.session_absolute_hours, ' h')}
        </Row>
        <Row label="API key maximum lifetime">{val(data.security?.api_key_max_days, ' days')}</Row>
        <Row label="Egress default allows private addresses">
          {yesNo(data.security?.egress_default_allow_private)}
        </Row>
      </Section>

      <Section id="set-limits" title="Execution limits">
        <Row label="Maximum run time">{val(data.limits?.max_run_seconds, ' s')}</Row>
        <Row label="Maximum tool calls per run">{val(data.limits?.max_tool_calls)}</Row>
        <Row label="Inline output maximum">
          {val(data.limits?.inline_output_max_bytes, ' bytes')}
        </Row>
      </Section>

      <Section id="set-workers" title="Workers">
        {data.workers == null ? (
          <Row label="Worker heartbeats">{NOT_REPORTED}</Row>
        ) : data.workers.length === 0 ? (
          <Row label="Worker heartbeats">None reporting</Row>
        ) : (
          data.workers.map((w) => (
            <Row key={w.name} label={w.name}>
              {w.status === 'alive' ? 'Alive' : 'Stale'} — last seen{' '}
              <time dateTime={w.last_seen_at}>{new Date(w.last_seen_at).toLocaleString()}</time>
            </Row>
          ))
        )}
      </Section>
    </div>
  );
}

export function PlatformSettingsView() {
  const q = useQuery({
    queryKey: installationKey,
    queryFn: () => endpoints.platformInstallation(),
    retry: false,
  });
  if (q.isPending)
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading platform settings...
      </p>
    );
  if (q.isError) {
    if (q.error instanceof ApiError && q.error.status === 403)
      return (
        <section aria-labelledby="set-denied">
          <h1 id="set-denied" className="text-2xl font-semibold text-slate-900">
            Platform Settings
          </h1>
          <p className="mt-2 text-sm text-slate-700">
            Platform settings are visible to platform administrators only.
          </p>
        </section>
      );
    return <QueryError error={q.error} />;
  }
  return <PlatformSettingsContent data={q.data} />;
}
