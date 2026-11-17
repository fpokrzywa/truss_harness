import { describe, expect, it } from 'vitest';
import type { RunEvent, Step } from './api/operate-types';
import { FAILURE_INFO, failureInfo } from './failure';
import {
  buildTimeline,
  canCancel,
  countdownText,
  groupSteps,
  isTerminal,
  newIdempotencyKey,
  runFilterParams,
  secondsUntil,
} from './run-utils';

const step = (over: Partial<Step>): Step => ({
  id: 's',
  run_id: 'r',
  seq: 1,
  kind: 'tool_call',
  name: 'crm.read',
  status: 'succeeded',
  attempt: 1,
  ...over,
});

describe('run state helpers', () => {
  it('cancel is allowed only for non-terminal statuses', () => {
    for (const s of ['pending', 'running', 'waiting_for_approval'] as const)
      expect(canCancel(s)).toBe(true);
    for (const s of ['completed', 'failed', 'cancelled', 'timed_out'] as const) {
      expect(canCancel(s)).toBe(false);
      expect(isTerminal(s)).toBe(true);
    }
  });

  it('drops blank filters from the query', () => {
    expect(runFilterParams({ status: 'failed', kind: '', agent_id: undefined, from: 'x' })).toEqual(
      {
        status: 'failed',
        from: 'x',
      },
    );
  });

  it('generates distinct v4 UUID idempotency keys', () => {
    const a = newIdempotencyKey();
    expect(a).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(newIdempotencyKey()).not.toBe(a);
  });

  it('formats approval expiry countdowns', () => {
    const now = Date.parse('2026-11-03T10:00:00Z');
    expect(secondsUntil('2026-11-03T10:01:05Z', now)).toBe(65);
    expect(countdownText(65)).toBe('1m 5s left');
    expect(countdownText(3700)).toBe('1h 1m left');
    expect(countdownText(0)).toBe('Expired');
    expect(countdownText(null)).toBe('No expiry');
  });
});

describe('timeline', () => {
  it('groups attempts per step ordered by attempt', () => {
    const groups = groupSteps([
      step({ id: 'b', seq: 2, name: 'second' }),
      step({ id: 'a2', seq: 1, attempt: 2 }),
      step({ id: 'a1', seq: 1, attempt: 1, status: 'failed', error_class: 'timeout' }),
    ]);
    expect(groups.map((g) => g.seq)).toEqual([1, 2]);
    expect(groups[0]?.attempts.map((a) => a.attempt)).toEqual([1, 2]);
    expect(groups[0]?.status).toBe('succeeded');
  });

  it('merges events and steps ordered by time then sequence', () => {
    const events: RunEvent[] = [
      { seq: 2, at: '2026-11-03T10:00:02Z', type: 'retry_scheduled', detail: { backoff_ms: 500 } },
      { seq: 1, at: '2026-11-03T10:00:00Z', type: 'state_change', from: 'pending', to: 'running' },
      { seq: 3, at: '2026-11-03T10:00:05Z', type: 'lease_lost' },
    ];
    const steps = [
      step({ id: 'a1', seq: 1, started_at: '2026-11-03T10:00:01Z', status: 'failed' }),
      step({ id: 'a2', seq: 1, attempt: 2, started_at: '2026-11-03T10:00:03Z' }),
    ];
    const t = buildTimeline(events, steps);
    expect(t.map((e) => (e.type === 'event' ? e.event.type : 'step'))).toEqual([
      'state_change',
      'step',
      'retry_scheduled',
      'lease_lost',
    ]);
  });
});

describe('failure explanations', () => {
  it('covers every error class with an explanation and action', () => {
    const classes = [
      'validation',
      'authz_denied',
      'budget_denied',
      'provider_unavailable',
      'rate_limited',
      'timeout',
      'transient_network',
      'provider_error_4xx',
      'tool_error',
      'schema_violation',
      'approval_rejected',
      'cancelled',
      'indeterminate_side_effect',
      'internal',
    ];
    expect(Object.keys(FAILURE_INFO).sort()).toEqual([...classes].sort());
    for (const c of classes) {
      expect(failureInfo(c)?.explanation.length).toBeGreaterThan(10);
      expect(failureInfo(c)?.action.length).toBeGreaterThan(5);
    }
  });

  it('marks transient classes retryable and others not', () => {
    for (const c of ['provider_unavailable', 'rate_limited', 'timeout', 'transient_network'])
      expect(failureInfo(c)?.retryable).toBe(true);
    for (const c of ['validation', 'authz_denied', 'tool_error', 'indeterminate_side_effect'])
      expect(failureInfo(c)?.retryable).toBe(false);
  });

  it('tolerates unknown classes and none', () => {
    expect(failureInfo(null)).toBeNull();
    expect(failureInfo('brand_new')?.title).toBe('brand_new');
  });
});
