'use client';

import { useEffect, useMemo, useState } from 'react';
import type { Run, RunEvent, Step } from '@/lib/api/operate-types';
import { useRunEvents, useRunSteps } from '@/lib/operate-hooks';
import {
  backoffText,
  buildTimeline,
  EVENT_LABEL,
  type StepGroup,
  type TimelineEntry,
} from '@/lib/run-utils';
import { Badge, formatDate, QueryGate } from '../admin/ui';
import { failureInfo } from '@/lib/failure';

/**
 * An indeterminate step is unknown only while its run has `outcome_unknown` (R1.4). Otherwise it
 * is the record of an attempt that was followed by an authorized retry (mark_succeeded and fail
 * move the step to succeeded/failed), so it is not called unknown and not failed, and nothing was
 * confirmed about that attempt. While the run still has an open decision
 * the step carries nothing to tell which of its indeterminate steps is the open one.
 */
const stepStatusLabel = (s: Step['status'], run?: Pick<Run, 'outcome_unknown'>): string =>
  s !== 'indeterminate' ? s : run && !run.outcome_unknown ? 'Retry authorized' : 'Outcome unknown';

const NEVER_CONFIRMED = ' (the outcome of this attempt was never confirmed)';
const unconfirmed = (s: Step['status'], run?: Pick<Run, 'outcome_unknown'>): string =>
  s === 'indeterminate' && run && !run.outcome_unknown ? NEVER_CONFIRMED : '';

const STEP_TONE = {
  succeeded: 'good',
  failed: 'bad',
  indeterminate: 'warn',
  running: 'warn',
  pending: 'neutral',
  skipped: 'neutral',
  cancelled: 'neutral',
} as const;

function compact(detail: Record<string, unknown> | null | undefined): string {
  if (!detail || Object.keys(detail).length === 0) return '';
  return Object.entries(detail)
    .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ');
}

function EventRow({ event }: { event: RunEvent }) {
  const label = EVENT_LABEL[event.type] ?? event.type;
  const warn = event.type === 'lease_lost' || event.type === 'recovered';
  const backoff = event.type === 'retry_scheduled' ? backoffText(event.detail) : null;
  return (
    <li
      data-testid="timeline-event"
      data-event-type={event.type}
      className={`rounded-md border px-3 py-2 text-sm ${
        warn ? 'border-amber-400 bg-amber-50' : 'border-slate-200 bg-white'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-slate-500">#{event.seq}</span>
        <span className="font-medium">
          {warn && <span aria-hidden="true">! </span>}
          {label}
        </span>
        {event.type === 'state_change' && (
          <span>
            {event.from ?? '-'} {'->'} {event.to ?? '-'}
          </span>
        )}
        {backoff && <Badge tone="warn">Backoff {backoff}</Badge>}
        <time dateTime={event.at} className="ml-auto text-xs text-slate-500">
          {formatDate(event.at)}
        </time>
      </div>
      {event.type !== 'state_change' && compact(event.detail) && (
        <p className="mt-1 text-xs break-words text-slate-700">{compact(event.detail)}</p>
      )}
    </li>
  );
}

function StepRow({
  group,
  run,
  onOpen,
}: {
  group: StepGroup;
  run?: Run;
  onOpen: (g: StepGroup) => void;
}) {
  return (
    <li
      data-testid="timeline-step"
      className="rounded-md border border-blue-200 bg-blue-50/40 px-3 py-2 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-slate-500">step {group.seq}</span>
        <span className="font-medium">{group.name}</span>
        <span className="text-xs text-slate-600">{group.kind}</span>
        <Badge tone={STEP_TONE[group.status] ?? 'neutral'}>
          {stepStatusLabel(group.status, run)}
        </Badge>
        {group.attempts.length > 1 && <Badge tone="warn">{group.attempts.length} attempts</Badge>}
        <button
          type="button"
          className="btn ml-auto px-2 py-0.5 text-xs"
          aria-label={`Details for step ${group.seq} ${group.name}`}
          onClick={() => onOpen(group)}
        >
          Details
        </button>
      </div>
      <ol aria-label={`Attempts for step ${group.seq}`} className="mt-1 space-y-0.5 pl-4 text-xs">
        {group.attempts.map((a) => (
          <li key={a.id} data-testid="step-attempt">
            Attempt {a.attempt}: {stepStatusLabel(a.status, run)}
            {unconfirmed(a.status, run)}
            {a.error_class ? ` (${a.error_class})` : ''}
            {a.started_at ? ` - ${formatDate(a.started_at)}` : ''}
          </li>
        ))}
      </ol>
    </li>
  );
}

function StepDrawer({ group, run, onClose }: { group: StepGroup; run?: Run; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <aside
      aria-label="Step details"
      className="fixed inset-y-0 right-0 z-20 w-full max-w-md space-y-3 overflow-y-auto border-l border-slate-300 bg-white p-4 shadow-xl"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-base font-semibold">
          Step {group.seq}: {group.name}
        </h3>
        <button type="button" className="btn" onClick={onClose} autoFocus>
          Close
        </button>
      </div>
      {group.attempts.map((a: Step) => {
        const f = failureInfo(a.error_class);
        return (
          <section key={a.id} aria-label={`Attempt ${a.attempt}`} className="card text-sm">
            <h4 className="font-semibold">Attempt {a.attempt}</h4>
            <dl className="mt-1 grid grid-cols-[8rem_1fr] gap-1 text-xs">
              <dt>Status</dt>
              <dd>
                {stepStatusLabel(a.status, run)}
                {unconfirmed(a.status, run)}
              </dd>
              <dt>Started</dt>
              <dd>{formatDate(a.started_at)}</dd>
              <dt>Finished</dt>
              <dd>{formatDate(a.finished_at)}</dd>
              {a.model_id && (
                <>
                  <dt>Model</dt>
                  <dd className="break-all">{a.model_id}</dd>
                </>
              )}
              {a.tool_version_id && (
                <>
                  <dt>Tool version</dt>
                  <dd className="break-all">{a.tool_version_id}</dd>
                </>
              )}
              {a.tool_invocation_id && (
                <>
                  <dt>Tool invocation</dt>
                  <dd className="break-all">{a.tool_invocation_id}</dd>
                </>
              )}
              {a.idempotency_key && (
                <>
                  <dt>Idempotency key</dt>
                  <dd className="break-all">{a.idempotency_key}</dd>
                </>
              )}
              {a.error_class && (
                <>
                  <dt>Error class</dt>
                  <dd>
                    {a.error_class}
                    {f ? ` - ${f.explanation}` : ''}
                  </dd>
                </>
              )}
              {a.usage && (
                <>
                  <dt>Usage</dt>
                  <dd>{compact(a.usage as Record<string, unknown>)}</dd>
                </>
              )}
            </dl>
            {(a.input_ref !== undefined || a.output_ref !== undefined) && (
              <pre
                tabIndex={0}
                aria-label={`Attempt ${a.attempt} references`}
                className="mt-2 max-h-48 overflow-auto rounded bg-slate-100 p-2 font-mono text-xs"
              >
                {JSON.stringify({ input_ref: a.input_ref, output_ref: a.output_ref }, null, 2)}
              </pre>
            )}
          </section>
        );
      })}
    </aside>
  );
}

export function TimelineList({ entries, run }: { entries: TimelineEntry[]; run?: Run }) {
  const [open, setOpen] = useState<StepGroup | null>(null);
  if (entries.length === 0)
    return <p className="p-2 text-sm text-slate-600">No events or steps recorded yet.</p>;
  return (
    <>
      <ol aria-label="Execution timeline" className="space-y-2">
        {entries.map((e) =>
          e.type === 'event' ? (
            <EventRow key={`e-${e.event.seq}`} event={e.event} />
          ) : (
            <StepRow key={`s-${e.group.seq}`} group={e.group} run={run} onOpen={setOpen} />
          ),
        )}
      </ol>
      {open && <StepDrawer group={open} run={run} onClose={() => setOpen(null)} />}
    </>
  );
}

/** Execution Timeline tab: events and steps merged by time and sequence, attempts grouped per step. */
export function Timeline({ org, run }: { org: string; run: Run }) {
  const events = useRunEvents(org, run.id, run);
  const steps = useRunSteps(org, run.id, run);
  const entries = useMemo(
    () => (events.data && steps.data ? buildTimeline(events.data, steps.data) : []),
    [events.data, steps.data],
  );
  return (
    <QueryGate query={events} noun="timeline">
      {() => (
        <QueryGate query={steps} noun="steps">
          {() => <TimelineList entries={entries} run={run} />}
        </QueryGate>
      )}
    </QueryGate>
  );
}
