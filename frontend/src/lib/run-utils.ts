import type { Approval, Run, RunEvent, RunFilters, RunStatus, Step } from './api/operate-types';

export const TERMINAL: readonly RunStatus[] = ['completed', 'failed', 'cancelled', 'timed_out'];
export const isTerminal = (s: RunStatus): boolean => TERMINAL.includes(s);

/** pending/running/waiting runs can be cancelled; terminal ones cannot. */
export const canCancel = (s: RunStatus): boolean => !isTerminal(s);

export const STATUS_LABEL: Record<RunStatus, string> = {
  pending: 'Pending',
  running: 'Running',
  waiting_for_approval: 'Waiting for approval',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  timed_out: 'Timed out',
};

/** Shown for any run with an open unknown-outcome decision, whatever its status (R1). */
export const UNKNOWN_OUTCOME_LABEL = 'Write outcome unknown';

/** Plain text for `waiting_reason`; the raw token `indeterminate_side_effect` is not shown to people. */
export function waitingText(r: Pick<Run, 'waiting_reason'>, fallback: string): string {
  if (r.waiting_reason === 'indeterminate_side_effect')
    return 'A write may have been delivered; its outcome is unknown until an operator decides.';
  return r.waiting_reason ?? fallback;
}

/** The question was asked and has since been answered; `error_message` names the verdict. */
export const isOutcomeAnswered = (r: Pick<Run, 'outcome_unknown' | 'error_class'>): boolean =>
  !r.outcome_unknown && r.error_class === 'indeterminate_side_effect';

/**
 * Retry of an unknown write is possible only while the run is still waiting on the decision and
 * its deadline has not passed (R1.3). Ended runs, and waiting runs past their deadline, can only
 * record `mark_succeeded` or `fail`.
 */
export function canRetryUnknownWrite(r: Pick<Run, 'status' | 'deadline_at'>, now: number): boolean {
  if (r.status !== 'waiting_for_approval') return false;
  const d = r.deadline_at ? Date.parse(r.deadline_at) : NaN;
  return Number.isNaN(d) || d > now;
}

export const STATUS_TONE: Record<RunStatus, 'neutral' | 'good' | 'warn' | 'bad'> = {
  pending: 'neutral',
  running: 'warn',
  waiting_for_approval: 'warn',
  completed: 'good',
  failed: 'bad',
  cancelled: 'neutral',
  timed_out: 'bad',
};

/** Query-string params for GET runs; blank filters are dropped. */
export function runFilterParams(f: RunFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(f)) if (v) out[k] = v;
  return out;
}

export const principalLabel = (
  p: { display?: string | null; id: string } | string | null | undefined,
): string => (!p ? '-' : typeof p === 'string' ? p : (p.display ?? p.id));

// ---- Timeline ------------------------------------------------------------------------------

export interface StepGroup {
  seq: number;
  name: string;
  kind: Step['kind'];
  attempts: Step[];
  /** Status of the final attempt. */
  status: Step['status'];
}

export type TimelineEntry =
  | { type: 'event'; at: string; order: number; event: RunEvent }
  | { type: 'step'; at: string; order: number; group: StepGroup };

/** Attempts of one logical step share `seq`; they are listed by attempt number. */
export function groupSteps(steps: Step[]): StepGroup[] {
  const bySeq = new Map<number, Step[]>();
  for (const s of steps) bySeq.set(s.seq, [...(bySeq.get(s.seq) ?? []), s]);
  return [...bySeq.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([seq, list]) => {
      const attempts = [...list].sort((a, b) => a.attempt - b.attempt);
      const last = attempts[attempts.length - 1] as Step;
      return { seq, name: last.name, kind: last.kind, attempts, status: last.status };
    });
}

const ts = (v: string | null | undefined): number => {
  const t = v ? Date.parse(v) : NaN;
  return Number.isNaN(t) ? 0 : t;
};

/** Merges events and grouped steps into one list ordered by time, then event seq / step seq. */
export function buildTimeline(events: RunEvent[], steps: Step[]): TimelineEntry[] {
  const out: TimelineEntry[] = [
    ...events.map((event): TimelineEntry => ({
      type: 'event',
      at: event.at,
      order: event.seq,
      event,
    })),
    ...groupSteps(steps).map((group): TimelineEntry => {
      const first = group.attempts[0] as Step;
      return {
        type: 'step',
        at: first.started_at ?? first.finished_at ?? '',
        order: group.seq,
        group,
      };
    }),
  ];
  return out.sort((a, b) => ts(a.at) - ts(b.at) || a.order - b.order);
}

export const EVENT_LABEL: Record<string, string> = {
  state_change: 'State change',
  step_started: 'Step started',
  step_finished: 'Step finished',
  gate_decision: 'Gate decision',
  approval_requested: 'Approval requested',
  approval_decided: 'Approval decided',
  retry_scheduled: 'Retry scheduled',
  lease_lost: 'Lease lost',
  recovered: 'Recovered by another worker',
  cancel_requested: 'Cancel requested',
};

/** Backoff text for a retry_scheduled event, tolerant of seconds/milliseconds detail keys. */
export function backoffText(detail: Record<string, unknown> | null | undefined): string | null {
  if (!detail) return null;
  const ms = detail.backoff_ms ?? detail.delay_ms;
  if (typeof ms === 'number') return `${ms} ms`;
  const s = detail.backoff_s ?? detail.delay_s;
  if (typeof s === 'number') return `${s} s`;
  return null;
}

// ---- Approvals -----------------------------------------------------------------------------

/** True when the current user started the run (decision buttons are hidden; server enforces too). */
export function isInitiator(
  approval: Approval,
  run: Run | undefined,
  userId: string | undefined,
): boolean {
  if (!userId) return false;
  const initiator = approval.run_initiated_by ?? run?.initiated_by;
  return initiator?.id === userId;
}

/** Whole seconds until `expires` (negative once past). */
export function secondsUntil(expires: string | null | undefined, now: number): number | null {
  if (!expires) return null;
  const t = Date.parse(expires);
  return Number.isNaN(t) ? null : Math.floor((t - now) / 1000);
}

export function countdownText(seconds: number | null): string {
  if (seconds === null) return 'No expiry';
  if (seconds <= 0) return 'Expired';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h > 0 ? `${h}h ${m}m left` : m > 0 ? `${m}m ${s}s left` : `${s}s left`;
}

// ---- Idempotency ---------------------------------------------------------------------------

/** RFC 4122 v4 UUID for the Idempotency-Key header. */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === 'function') return c.randomUUID();
  const b = new Uint8Array(16);
  c.getRandomValues(b);
  b[6] = ((b[6] as number) & 0x0f) | 0x40;
  b[8] = ((b[8] as number) & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
