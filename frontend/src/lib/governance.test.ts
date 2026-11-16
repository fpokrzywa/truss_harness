import { describe, expect, it } from 'vitest';
import type { UsageEvent } from './api/governance-types';
import {
  costKindLabel,
  dailyTokens,
  eventCostTotals,
  floorFor,
  formatMoney,
  niceTicks,
  normalizeCost,
  normalizeOverview,
  normalizeRetention,
  normalizeRetentionPreview,
  normalizeSummary,
  parseThresholds,
  snapshotVersion,
  thresholdsText,
} from './governance';

describe('costKindLabel', () => {
  it('uses the contract wording for every kind', () => {
    expect(costKindLabel('provider_reported')).toBe('Provider-reported');
    expect(costKindLabel('estimated_infra')).toBe('Estimated infrastructure');
    expect(costKindLabel('calculated', { versions: [3] })).toBe(
      'Calculated from rate card v3 (not an invoice)',
    );
    expect(costKindLabel('calculated', { versions: [2, 3] })).toBe(
      'Calculated from 2 rate card versions (not an invoice)',
    );
    expect(costKindLabel('calculated')).toBe('Calculated from rate card (not an invoice)');
    expect(costKindLabel('unmeasured', { count: 1 })).toBe('Unmeasured (1 event)');
    expect(costKindLabel('unmeasured', { count: 12 })).toBe('Unmeasured (12 events)');
  });
});

describe('normalizers', () => {
  it('derives total_known from the kinds when the server omits it', () => {
    expect(
      normalizeCost({ provider_reported: 1, calculated: 2, estimated_infra: 0.5 }).total_known,
    ).toBe(3.5);
  });

  it('never claims reconciliation without an explicit true', () => {
    expect(normalizeSummary({ groups: [] }).reconciliation.consistent).toBe(false);
    expect(
      normalizeSummary({ groups: [], reconciliation: { consistent: 'yes' } }).reconciliation
        .consistent,
    ).toBe(false);
    expect(
      normalizeSummary({ groups: [], reconciliation: { consistent: true } }).reconciliation
        .consistent,
    ).toBe(true);
  });

  it('derives totals per kind from groups when absent', () => {
    const s = normalizeSummary({
      groups: [
        { key: 'a', calls: 1, cost: { calculated: 1, unmeasured_count: 1 }, currency: 'USD' },
        { key: 'b', calls: 2, cost: { calculated: 2, provider_reported: 4 } },
      ],
    });
    expect(s.totals.calls).toBe(3);
    expect(s.totals.cost).toMatchObject({
      calculated: 3,
      provider_reported: 4,
      unmeasured_count: 1,
      total_known: 7,
    });
  });
});

describe('formatMoney', () => {
  it('keeps sub-cent amounts and falls back for odd currency codes', () => {
    expect(formatMoney(0.000123, 'USD')).toContain('0.000123');
    expect(formatMoney(5, 'TOKENS')).toBe('5.00 TOKENS');
  });
});

describe('ledger helpers', () => {
  const e = (over: Partial<UsageEvent>): UsageEvent => ({
    id: 'x',
    occurred_at: '2026-11-01T10:00:00Z',
    event_type: 'model_call',
    cost_kind: 'calculated',
    ...over,
  });

  it('sums events per kind and counts unmeasured separately (no amount invented)', () => {
    const c = eventCostTotals([
      e({ cost_amount: 1 }),
      e({ cost_amount: 0.5, cost_kind: 'provider_reported' }),
      e({ cost_amount: null, cost_kind: 'unmeasured' }),
      e({ cost_amount: -0.25, corrects_event_id: 'x' }), // compensating row
    ]);
    expect(c).toEqual({
      provider_reported: 0.5,
      calculated: 0.75,
      estimated_infra: 0,
      unmeasured_count: 1,
      total_known: 1.25,
    });
  });

  it('buckets tokens per UTC day in order', () => {
    expect(
      dailyTokens([
        e({ occurred_at: '2026-11-02T01:00:00Z', input_tokens: 1 }),
        e({ occurred_at: '2026-11-01T23:00:00Z', input_tokens: 2, output_tokens: 3 }),
        e({ occurred_at: '2026-11-01T01:00:00Z', input_tokens: 4 }),
      ]),
    ).toEqual([
      { day: '2026-11-01', input_tokens: 6, output_tokens: 3, events: 2 },
      { day: '2026-11-02', input_tokens: 1, output_tokens: 0, events: 1 },
    ]);
  });

  it('reads the snapshot version, else the rate card version id', () => {
    expect(snapshotVersion(e({ rate_snapshot: { version_no: 5 } }))).toBe('5');
    expect(snapshotVersion(e({ rate_snapshot: {}, rate_card_version_id: 'rcv' }))).toBe('rcv');
    expect(snapshotVersion(e({}))).toBeNull();
  });
});

describe('budget and chart helpers', () => {
  it('parses percentage thresholds into sorted unique fractions', () => {
    expect(parseThresholds('80, 50,100, 50')).toEqual([0.5, 0.8, 1]);
    expect(parseThresholds('')).toEqual([]);
    expect(parseThresholds('a')).toBeNull();
    expect(parseThresholds('0')).toBeNull();
    expect(thresholdsText([0.5, 0.8, 1])).toBe('50, 80, 100');
  });

  it('produces round ticks covering the maximum', () => {
    expect(niceTicks(0)).toEqual([0, 1]);
    const t = niceTicks(87);
    expect(t[0]).toBe(0);
    expect(t.at(-1) as number).toBeGreaterThanOrEqual(87);
    expect(t.every((v) => Number.isInteger(v))).toBe(true);
  });

  it('knows the audit retention floor from the contract', () => {
    expect(floorFor({ data_class: 'audit_events' })).toBe(90);
    expect(floorFor({ data_class: 'usage_events' })).toBeNull();
    expect(floorFor({ data_class: 'usage_events', floor_days: 14 })).toBe(14);
  });
});

describe('backend (G1) shapes', () => {
  it('reads rate card versions sent as {id, version_no} objects', () => {
    const s = normalizeSummary({
      groups: [{ key: 'a', rate_card_versions: [{ id: 'v-id', version_no: 3 }] }],
      totals: { rate_card_versions: [{ id: 'v-id', version_no: 3 }, { id: 'x' }] },
    });
    expect(s.groups[0]?.rate_card_versions).toEqual([3]);
    expect(s.totals.rate_card_versions).toEqual([3]);
  });

  it('surfaces mismatched_rows and the reconciliation level', () => {
    const s = normalizeSummary({
      groups: [],
      reconciliation: {
        events_sum: 10,
        rollup_sum: 9,
        consistent: false,
        mismatched_rows: 2,
        level: 'full',
      },
    });
    expect(s.reconciliation).toEqual({
      events_sum: 10,
      rollup_sum: 9,
      consistent: false,
      mismatched_rows: 2,
      level: 'full',
    });
  });

  it('prefers cost_total_known for top spenders', () => {
    const o = normalizeOverview({
      top_spenders: [
        { key: 'p', cost_total_known: 9, cost: { calculated: 1, total_known: 1 }, tokens: 5 },
        { key: 'q', cost: { calculated: 2 } },
      ],
    });
    expect(o.top_spenders[0]?.cost_total_known).toBe(9);
    expect(o.top_spenders[0]?.cost?.total_known).toBe(9);
    expect(o.top_spenders[1]?.cost_total_known).toBe(2);
  });

  it('treats retention as not enforced unless the server says otherwise', () => {
    expect(normalizeRetention({ items: [] }).enforcement).toBe('not_implemented');
    expect(
      normalizeRetention({ items: [], enforcement: 'not_implemented', note: 'n' }),
    ).toMatchObject({ enforcement: 'not_implemented', note: 'n' });
    expect(normalizeRetentionPreview({ items: [{ data_class: 'a', count: 2 }] })).toMatchObject({
      enforcement: 'not_implemented',
      items: [{ data_class: 'a', count: 2 }],
    });
  });
});
