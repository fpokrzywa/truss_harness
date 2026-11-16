import { screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubGov } from '@/test/governance';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { RunCostView } from './RunCostView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const ev = (over: Record<string, unknown>) => ({
  id: 'e',
  occurred_at: '2026-11-03T10:00:00Z',
  event_type: 'model_call',
  step_id: 'draft',
  attempt: 1,
  model_id: 'fake-chat',
  input_tokens: 100,
  output_tokens: 20,
  duration_ms: 900,
  cost_amount: 0.5,
  currency: 'USD',
  cost_kind: 'calculated',
  rate_card_version_id: 'rcv-9',
  rate_snapshot: { version_no: 3, input_rate: 1.5, output_rate: 6, unit: 'per_1m_tokens' },
  ...over,
});

describe('RunCostView', () => {
  it('lists every attempt of a retried step separately with its own rate snapshot', async () => {
    stubGov({
      'GET /api/v1/orgs/acme/usage/runs/run-1': () =>
        jsonResponse({
          run_id: 'run-1',
          events: [
            ev({ id: 'e1', attempt: 1, cost_amount: 0.5 }),
            ev({ id: 'e2', attempt: 2, cost_amount: 0.75, rate_snapshot: { version_no: 4 } }),
            ev({
              id: 'e3',
              step_id: 'summarize',
              attempt: 1,
              cost_kind: 'unmeasured',
              cost_amount: null,
              rate_snapshot: null,
              rate_card_version_id: null,
            }),
            ev({
              id: 'e4',
              step_id: 'summarize',
              attempt: 2,
              cost_kind: 'provider_reported',
              cost_amount: 0.1,
            }),
          ],
        }),
    });
    renderWithClient(<RunCostView id="run-1" />);
    const table = await screen.findByRole('table', { name: 'Usage events by step and attempt' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(4); // retries are not merged
    expect(within(rows[0] as HTMLElement).getByText('Attempt 1')).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText('Attempt 2')).toBeInTheDocument();
    // Calculated rows name the rate card version they used.
    expect(
      within(rows[0] as HTMLElement).getByText('Calculated from rate card v3 (not an invoice)'),
    ).toBeInTheDocument();
    expect(
      within(rows[1] as HTMLElement).getByText('Calculated from rate card v4 (not an invoice)'),
    ).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText('Unmeasured (1 event)')).toBeInTheDocument();
    expect(within(rows[3] as HTMLElement).getByText('Provider-reported')).toBeInTheDocument();
    // Snapshot details are available per event.
    expect(within(rows[0] as HTMLElement).getByText('rate_card_version_id')).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText('per_1m_tokens')).toBeInTheDocument();
  });

  it('totals per kind without an unlabeled sum', async () => {
    stubGov({
      'GET /api/v1/orgs/acme/usage/runs/run-1': () =>
        jsonResponse([
          ev({ id: 'e1', cost_amount: 0.5 }),
          ev({ id: 'e2', attempt: 2, cost_amount: 0.25 }),
          ev({ id: 'e3', cost_kind: 'provider_reported', cost_amount: 1 }),
        ]),
    });
    renderWithClient(<RunCostView id="run-1" />);
    const summary = await screen.findByRole('region', { name: 'Run cost summary' });
    expect(within(summary).getByText(/Calculated from rate card v3/)).toBeInTheDocument();
    expect(within(summary).getByText('Provider-reported')).toBeInTheDocument();
    expect(within(summary).getByText('Total known')).toBeInTheDocument();
    expect(within(summary).getByText(/1\.75/)).toBeInTheDocument(); // 0.5 + 0.25 + 1, labelled "Total known"
  });

  it('marks compensating correction rows', async () => {
    stubGov({
      'GET /api/v1/orgs/acme/usage/runs/run-1': () =>
        jsonResponse([ev({ id: 'c1', corrects_event_id: 'e1', cost_amount: -0.5 })]),
    });
    renderWithClient(<RunCostView id="run-1" />);
    expect(await screen.findByText('Correction of e1')).toBeInTheDocument();
  });

  it('shows an empty state for a run without usage', async () => {
    stubGov({ 'GET /api/v1/orgs/acme/usage/runs/run-1': () => jsonResponse({ events: [] }) });
    renderWithClient(<RunCostView id="run-1" />);
    expect(await screen.findByText(/No usage events were recorded/)).toBeInTheDocument();
  });
});
