'use client';

import { useQuery } from '@tanstack/react-query';
import { endpoints } from '@/lib/api/endpoints';
import type { HealthDetails } from '@/lib/api/governance-types';
import { formatCount } from '@/lib/governance';
import { ApiError } from '@/lib/api/errors';
import { QueryError } from '../QueryError';

/** Plain-language statement of what the reported exporter mode actually does. */
export function exporterText(t: HealthDetails['telemetry']): string {
  switch (t.exporter) {
    case 'otlp':
      return `Traces are exported over OTLP${t.otlp?.endpoint ? ` to ${t.otlp.endpoint}` : ''}.`;
    case 'console':
      return 'Traces are written to the server console only. Nothing is sent to a collector.';
    case 'none':
      return 'Tracing export is off: spans are kept in a small in-memory buffer only. Nothing is sent to a collector.';
    default:
      return `Exporter mode: ${t.exporter}.`;
  }
}

export function TelemetryDetails({ details }: { details: HealthDetails }) {
  const t = details.telemetry;
  return (
    <section aria-label="Telemetry and queue" className="card">
      <h2 className="text-sm font-semibold">Telemetry and queue</h2>
      <dl className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
        <dt className="text-slate-700">Trace exporter</dt>
        <dd>
          <code className="font-mono">{t.exporter}</code>
        </dd>
        {t.otlp && (
          <>
            <dt className="text-slate-700">OTLP spans exported / failed</dt>
            <dd>
              {formatCount(t.otlp.exported)} / {formatCount(t.otlp.failed)}
            </dd>
          </>
        )}
        <dt className="text-slate-700">Buffered spans</dt>
        <dd>{formatCount(t.buffered_spans)}</dd>
        <dt className="text-slate-700">Jobs queued / leased</dt>
        <dd>
          {formatCount(details.queue.queued)} / {formatCount(details.queue.leased)}
        </dd>
        <dt className="text-slate-700">Live workers</dt>
        <dd>{formatCount(details.workers.running)}</dd>
      </dl>
      <p className="mt-2 text-xs text-slate-600">{exporterText(t)}</p>
      {t.metrics && <p className="text-xs text-slate-600">Metrics: {t.metrics}</p>}
    </section>
  );
}

/** Platform-admin details from `/api/v1/platform/health-details`. Hidden when not permitted. */
export function TelemetryPanel() {
  const q = useQuery({
    queryKey: ['platform', 'health-details'],
    queryFn: () => endpoints.platformHealthDetails(),
    retry: false,
  });
  if (q.isPending)
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading telemetry details...
      </p>
    );
  if (q.isError) {
    if (q.error instanceof ApiError && (q.error.status === 403 || q.error.status === 404))
      return (
        <p className="text-sm text-slate-600">
          Telemetry and queue details are visible to platform administrators only.
        </p>
      );
    return <QueryError error={q.error} />;
  }
  return <TelemetryDetails details={q.data} />;
}
