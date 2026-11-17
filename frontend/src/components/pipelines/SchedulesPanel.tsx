'use client';

import { ApiError } from '@/lib/api/errors';
import { usePipelineSchedules } from '@/lib/pipeline-hooks';
import { formatDate } from '../admin/ui';
import { QueryError } from '../QueryError';

/**
 * Scheduling is not enabled in the MVP (OD-8). The section is read-only: it explains the 501
 * `scheduling_not_enabled` answer and never offers schedule creation controls.
 */
export function SchedulesPanel({ org, pipelineId }: { org: string; pipelineId: string }) {
  const query = usePipelineSchedules(org, pipelineId);
  const disabled =
    query.error instanceof ApiError &&
    (query.error.code === 'scheduling_not_enabled' || query.error.status === 501);
  return (
    <section aria-label="Schedules" className={`card space-y-2 ${disabled ? 'opacity-80' : ''}`}>
      <h3 className="text-sm font-semibold">Schedules</h3>
      {query.isPending && <p role="status">Checking scheduling...</p>}
      {disabled && (
        <p role="status" className="text-sm text-slate-700">
          Scheduling is not enabled in this installation. Pipelines run on demand until an
          administrator enables pipeline schedules.
        </p>
      )}
      {query.isError && !disabled && <QueryError error={query.error} />}
      {query.data &&
        (query.data.length === 0 ? (
          <p className="text-sm text-slate-600">No schedules.</p>
        ) : (
          <ul className="list-disc pl-5 text-sm">
            {query.data.map((s, i) => (
              <li key={s.id ?? i}>
                {s.cron ?? 'schedule'}
                {s.next_run_at ? `, next run ${formatDate(s.next_run_at)}` : ''}
                {s.status === 'paused' || s.enabled === false ? ' (paused)' : ''}
              </li>
            ))}
          </ul>
        ))}
    </section>
  );
}
