import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GOV_PERMS, stubGov } from '@/test/governance';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { BudgetsView, UtilizationBar } from './BudgetsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const B = '/api/v1/orgs/acme/budgets';
const budget = {
  id: 'b1',
  scope: { type: 'tenant', id: 'org-1' },
  period: 'monthly',
  amount: 100,
  currency: 'USD',
  metric: 'cost',
  enforcement: 'hard',
  on_exceed: 'deny',
  alert_thresholds: [0.5, 0.8, 1],
  status: 'active',
  row_version: 3,
};
const status = {
  period_start: '2026-11-01',
  reserved: 5,
  settled: 70,
  remaining: 25,
  utilization: 0.75,
  alerts_fired: [0.5],
  on_exceed_effective: 'deny',
  note: null,
};

describe('UtilizationBar', () => {
  it('exposes utilization as a meter with threshold markers and text for reached thresholds', () => {
    render(<UtilizationBar utilization={0.85} thresholds={[0.5, 0.8, 1]} label="Spend" />);
    const meter = screen.getByRole('meter', { name: 'Spend' });
    expect(meter).toHaveAttribute('aria-valuenow', '85');
    expect(screen.getByTestId('threshold-50')).toBeInTheDocument();
    expect(screen.getByTestId('threshold-80')).toBeInTheDocument();
    expect(screen.getByTestId('threshold-100')).toBeInTheDocument();
    // Reached thresholds are stated in text, not only by color; the unreached one has no suffix.
    expect(screen.getByText(/85% used/)).toHaveTextContent(
      '85% used; thresholds 50% (reached), 80% (reached), 100%',
    );
  });

  it('scales past 100% so an overrun stays visible', () => {
    render(<UtilizationBar utilization={1.2} thresholds={[1]} label="Over" />);
    expect(screen.getByRole('meter', { name: 'Over' })).toHaveAttribute('aria-valuenow', '120');
    expect(screen.getByText(/120% used/)).toHaveTextContent('120% used; thresholds 100% (reached)');
  });
});

describe('BudgetsView', () => {
  it('shows budgets with status utilization from budgets/{id}/status', async () => {
    stubGov({
      [`GET ${B}`]: () => list([budget]),
      [`GET ${B}/b1/status`]: () => jsonResponse(status),
      'GET /api/v1/orgs/acme/quotas': () => list([]),
    });
    renderWithClient(<BudgetsView />);
    const meter = await screen.findByRole('meter', {
      name: /Utilization of Organization monthly cost budget/,
    });
    expect(meter).toHaveAttribute('aria-valuenow', '75');
    expect(screen.getByText(/alerts fired: 50%/)).toBeInTheDocument();
    expect(screen.getByText(/75% used/)).toHaveTextContent('thresholds 50% (reached), 80%, 100%');
    expect(screen.getByText('No quotas defined.')).toBeInTheDocument();
  });

  it('creates a budget with the contract payload (tokens metric, thresholds as fractions)', async () => {
    const fetchMock = stubGov({
      [`GET ${B}`]: () => list([]),
      [`POST ${B}`]: () => jsonResponse({ ...budget, id: 'b2' }, { status: 201 }),
      'GET /api/v1/orgs/acme/quotas': () => list([]),
    });
    renderWithClient(<BudgetsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create budget' }));
    const dialog = screen.getByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Metric'), 'tokens');
    await user.selectOptions(within(dialog).getByLabelText('Period'), 'daily');
    await user.type(within(dialog).getByLabelText('Amount (tokens)'), '2500000');
    await user.selectOptions(within(dialog).getByLabelText('Enforcement'), 'soft');
    await user.selectOptions(within(dialog).getByLabelText('On exceed'), 'require_approval');
    const th = within(dialog).getByLabelText('Alert thresholds (percent)');
    await user.clear(th);
    await user.type(th, '60, 90, 100');
    await user.click(within(dialog).getByRole('button', { name: 'Create budget' }));

    await waitFor(() => expect(bodyOf(fetchMock, `POST ${B}`)).toBeDefined());
    expect(bodyOf(fetchMock, `POST ${B}`)).toEqual({
      scope: { type: 'tenant', id: 'org-1' },
      period: 'daily',
      amount: 2500000,
      metric: 'tokens',
      enforcement: 'soft',
      on_exceed: 'require_approval',
      alert_thresholds: [0.6, 0.9, 1],
      status: 'active',
    });
  });

  it('validates the amount and thresholds before enabling save', async () => {
    stubGov({ [`GET ${B}`]: () => list([]), 'GET /api/v1/orgs/acme/quotas': () => list([]) });
    renderWithClient(<BudgetsView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create budget' }));
    const dialog = screen.getByRole('dialog');
    const save = within(dialog).getByRole('button', { name: 'Create budget' });
    expect(save).toBeDisabled();
    await user.type(within(dialog).getByLabelText('Amount'), '-5');
    expect(within(dialog).getByText('Enter a positive number.')).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText('Amount'));
    await user.type(within(dialog).getByLabelText('Amount'), '10');
    await user.clear(within(dialog).getByLabelText('Alert thresholds (percent)'));
    await user.type(within(dialog).getByLabelText('Alert thresholds (percent)'), 'abc');
    expect(save).toBeDisabled();
    expect(within(dialog).getByText(/Enter percentages separated by commas/)).toBeInTheDocument();
  });

  it('edits with PATCH including row_version', async () => {
    const fetchMock = stubGov({
      [`GET ${B}`]: () => list([budget]),
      [`GET ${B}/b1/status`]: () => jsonResponse(status),
      [`PATCH ${B}/b1`]: () => jsonResponse(budget),
      'GET /api/v1/orgs/acme/quotas': () => list([]),
    });
    renderWithClient(<BudgetsView />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', { name: /Edit budget Organization monthly/ }),
    );
    const dialog = screen.getByRole('dialog');
    const amount = within(dialog).getByLabelText('Amount');
    await user.clear(amount);
    await user.type(amount, '150');
    await user.click(within(dialog).getByRole('button', { name: 'Save budget' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `PATCH ${B}/b1`)).toBe(true));
    const body = bodyOf(fetchMock, `PATCH ${B}/b1`) as Record<string, unknown>;
    expect(body).toMatchObject({
      amount: 150,
      row_version: 3,
      alert_thresholds: [0.5, 0.8, 1],
    });
    // scope and metric are immutable server-side, so they are never sent on PATCH.
    expect(body).not.toHaveProperty('scope');
    expect(body).not.toHaveProperty('metric');
  });

  it('shows the effective on-exceed behaviour when it differs from the stored one', async () => {
    stubGov({
      [`GET ${B}`]: () =>
        list([{ ...budget, on_exceed: 'queue_until_reset', on_exceed_effective: 'deny' }]),
      [`GET ${B}/b1/status`]: () => jsonResponse(status),
      'GET /api/v1/orgs/acme/quotas': () => list([]),
    });
    renderWithClient(<BudgetsView />);
    expect(await screen.findByText(/Effective: deny/)).toHaveTextContent(
      'queue until reset is not applied yet',
    );
  });

  it('says a soft budget only alerts and never blocks work', async () => {
    stubGov({
      [`GET ${B}`]: () =>
        list([
          {
            ...budget,
            enforcement: 'soft',
            on_exceed: 'queue_until_reset',
            on_exceed_effective: 'alert_only',
          },
        ]),
      [`GET ${B}/b1/status`]: () => jsonResponse(status),
      'GET /api/v1/orgs/acme/quotas': () => list([]),
    });
    renderWithClient(<BudgetsView />);
    const note = await screen.findByText(/Effective: alert only/);
    expect(note).toHaveTextContent('never blocks work');
    expect(screen.queryByText(/Effective: deny/)).not.toBeInTheDocument();
  });

  it('shows a legacy run-count budget in runs, not tokens', async () => {
    stubGov({
      [`GET ${B}`]: () =>
        list([{ ...budget, metric: 'runs', amount: 5, currency: null, on_exceed: 'deny' }]),
      [`GET ${B}/b1/status`]: () => jsonResponse(status),
      'GET /api/v1/orgs/acme/quotas': () => list([]),
    });
    renderWithClient(<BudgetsView />);
    expect(await screen.findByText('5 runs')).toBeInTheDocument();
    expect(screen.queryByText('5 tokens')).not.toBeInTheDocument();
  });

  it('hides create/edit without budget:manage', async () => {
    stubGov(
      {
        [`GET ${B}`]: () => list([budget]),
        [`GET ${B}/b1/status`]: () => jsonResponse(status),
        'GET /api/v1/orgs/acme/quotas': () => list([]),
      },
      GOV_PERMS.filter((p) => p !== 'budget:manage'),
    );
    renderWithClient(<BudgetsView />);
    await screen.findByRole('meter');
    expect(screen.queryByRole('button', { name: 'Create budget' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit budget/ })).not.toBeInTheDocument();
  });

  it('lists and creates quotas', async () => {
    const fetchMock = stubGov({
      [`GET ${B}`]: () => list([]),
      'GET /api/v1/orgs/acme/quotas': () =>
        list([
          { id: 'q1', scope: { type: 'tenant', id: 'org-1' }, metric: 'runs_per_day', limit: 500 },
        ]),
      'POST /api/v1/orgs/acme/quotas': () => jsonResponse({ id: 'q2' }, { status: 201 }),
    });
    renderWithClient(<BudgetsView />);
    const user = userEvent.setup();
    expect(await screen.findByText('runs per day')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Create quota' }));
    const dialog = screen.getByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Quota metric'), 'concurrent_runs');
    await user.type(within(dialog).getByLabelText('Limit'), '4');
    await user.click(within(dialog).getByRole('button', { name: 'Create quota' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === 'POST /api/v1/orgs/acme/quotas')).toBe(true),
    );
    expect(bodyOf(fetchMock, 'POST /api/v1/orgs/acme/quotas')).toEqual({
      scope: { type: 'tenant', id: 'org-1' },
      metric: 'concurrent_runs',
      limit: 4,
    });
  });
});
