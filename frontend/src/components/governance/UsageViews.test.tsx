import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { COST, GOV_PERMS, queryOf, stubGov, SUMMARY } from '@/test/governance';
import { list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { CostExplorerView, ModelConsumptionView, UsageView } from './UsageViews';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const SUMMARY_KEY = 'GET /api/v1/orgs/acme/usage/summary';
const EVENTS_KEY = 'GET /api/v1/orgs/acme/usage/events';

describe('Cost Explorer honesty rules', () => {
  it('labels every cost kind, shows "Total known" and never an unlabeled total', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse(SUMMARY) });
    renderWithClient(<CostExplorerView />);
    const summary = await screen.findByRole('region', { name: 'Cost summary' });
    expect(within(summary).getByText('Provider-reported')).toBeInTheDocument();
    expect(
      within(summary).getByText('Calculated from rate card v2 (not an invoice)'),
    ).toBeInTheDocument();
    expect(within(summary).getByText('Estimated infrastructure')).toBeInTheDocument();
    expect(within(summary).getByText('Unmeasured (3 events)')).toBeInTheDocument();
    expect(within(summary).getByText('Total known')).toBeInTheDocument();
    expect(
      within(summary).getByText(/Excludes unmeasured events. Not an invoice/),
    ).toBeInTheDocument();
    // Amounts are rendered under their labels.
    expect(within(summary).getByText(/1\.50/)).toBeInTheDocument();
    expect(within(summary).getByText(/2\.25/)).toBeInTheDocument();

    // Table columns carry the kind labels; no bare "Cost" / "Total cost" header anywhere.
    const table = screen.getByRole('table', { name: 'Cost by project' });
    const heads = within(table)
      .getAllByRole('columnheader')
      .map((h) => h.textContent);
    expect(heads).toContain('Provider-reported');
    expect(heads).toContain('Calculated from rate card v2 (not an invoice)');
    expect(heads).toContain('Estimated infrastructure');
    expect(heads).toContain('Total known (not an invoice)');
    expect(heads.some((h) => /^(total )?cost$/i.test(h ?? ''))).toBe(false);
    expect(screen.queryByText(/^total cost$/i)).not.toBeInTheDocument();
  });

  it('shows the reconciliation badge, and flags a mismatch', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse(SUMMARY) });
    const { unmount } = renderWithClient(<CostExplorerView />);
    expect(await screen.findByText('Reconciled: consistent')).toBeInTheDocument();
    unmount();

    stubGov({
      [SUMMARY_KEY]: () =>
        jsonResponse({
          ...SUMMARY,
          reconciliation: { events_sum: 4, rollup_sum: 5, consistent: false },
        }),
    });
    renderWithClient(<CostExplorerView />);
    expect(await screen.findByText('Reconciliation mismatch')).toBeInTheDocument();
  });

  it('treats a missing consistent flag as not consistent', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse({ ...SUMMARY, reconciliation: {} }) });
    renderWithClient(<CostExplorerView />);
    expect(await screen.findByText('Reconciliation mismatch')).toBeInTheDocument();
  });

  it('draws the stacked chart with a legend of labelled kinds and a data table alternative', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse(SUMMARY) });
    renderWithClient(<CostExplorerView />);
    const legend = await screen.findByRole('list', { name: 'Legend' });
    expect(within(legend).getByText('Provider-reported')).toBeInTheDocument();
    expect(
      within(legend).getByText('Calculated from rate card v2 (not an invoice)'),
    ).toBeInTheDocument();
    expect(within(legend).getByText('Estimated infrastructure')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Stacked bars of known cost/ })).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Show data table' }));
    expect(screen.queryByRole('img', { name: /Stacked bars/ })).not.toBeInTheDocument();
    expect(
      screen.getByRole('table', { name: 'Chart data: known cost by group and cost kind' }),
    ).toBeInTheDocument();
  });

  it('changes the query when group-by and filters change', async () => {
    const fetchMock = stubGov({
      [SUMMARY_KEY]: () => jsonResponse(SUMMARY),
      'GET /api/v1/orgs/acme/workspaces': () => list([{ id: 'w1', slug: 'eng', name: 'Eng' }]),
      'GET /api/v1/orgs/acme/workspaces/eng/projects': () => list([]),
    });
    renderWithClient(<CostExplorerView />);
    await screen.findByText('Reconciled: consistent');
    expect(queryOf(fetchMock.calls, SUMMARY_KEY).get('group_by')).toBe('project');

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText('Group by'), 'agent');
    await waitFor(() =>
      expect(queryOf(fetchMock.calls, SUMMARY_KEY).get('group_by')).toBe('agent'),
    );

    await user.selectOptions(screen.getByLabelText('Cost kind'), 'calculated');
    await user.type(screen.getByLabelText('From date'), '2026-11-01');
    await waitFor(() => {
      const q = queryOf(fetchMock.calls, SUMMARY_KEY);
      expect(q.get('cost_kind')).toBe('calculated');
      expect(q.get('from')).toMatch(/^2026-11-0[1-2]T/);
      expect(q.has('to')).toBe(false);
      expect(q.has('workspace')).toBe(false);
    });
    await user.selectOptions(await screen.findByLabelText('Workspace'), 'eng');
    await waitFor(() => expect(queryOf(fetchMock.calls, SUMMARY_KEY).get('workspace')).toBe('eng'));
  });

  it('links run groups to the workflow cost detail', async () => {
    stubGov({
      [SUMMARY_KEY]: () =>
        jsonResponse({
          ...SUMMARY,
          groups: [{ ...SUMMARY.groups[0], key: 'run-42', label: null }],
        }),
    });
    renderWithClient(<CostExplorerView />);
    const user = userEvent.setup();
    await screen.findByText('Reconciled: consistent');
    await user.selectOptions(screen.getByLabelText('Group by'), 'run');
    expect(await screen.findByRole('link', { name: 'run-42' })).toHaveAttribute(
      'href',
      '/o/acme/govern/runs/run-42/cost',
    );
  });
});

describe('CSV export gating', () => {
  it('offers the export only with data:export, carrying the current filters', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse(SUMMARY) }, [...GOV_PERMS, 'data:export']);
    renderWithClient(<CostExplorerView />);
    const link = await screen.findByRole('link', { name: 'Export CSV' });
    const href = new URL(link.getAttribute('href') ?? '', 'http://x');
    expect(href.pathname).toBe('/api/v1/orgs/acme/usage/export');
    expect(href.searchParams.get('format')).toBe('csv');
    expect(href.searchParams.get('group_by')).toBe('project');
  });

  it('hides the export without data:export', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse(SUMMARY) });
    renderWithClient(<CostExplorerView />);
    await screen.findByText('Reconciled: consistent');
    expect(screen.queryByRole('link', { name: 'Export CSV' })).not.toBeInTheDocument();
  });
});

describe('Token Usage', () => {
  const events = [
    {
      id: 'e1',
      occurred_at: '2026-11-01T10:00:00Z',
      event_type: 'model_call',
      input_tokens: 100,
      output_tokens: 50,
      cost_kind: 'calculated',
    },
    {
      id: 'e2',
      occurred_at: '2026-11-01T12:00:00Z',
      event_type: 'model_call',
      input_tokens: 40,
      output_tokens: 10,
      cost_kind: 'calculated',
    },
    {
      id: 'e3',
      occurred_at: '2026-11-02T09:00:00Z',
      event_type: 'model_call',
      input_tokens: 10,
      output_tokens: 5,
      cost_kind: 'unmeasured',
    },
  ];

  it('queries by the chosen dimension and charts daily tokens with a table alternative', async () => {
    const fetchMock = stubGov({
      [SUMMARY_KEY]: () => jsonResponse(SUMMARY),
      [EVENTS_KEY]: () => list(events),
    });
    renderWithClient(<UsageView />);
    expect(
      await screen.findByRole('img', { name: /Daily input and output tokens/ }),
    ).toBeInTheDocument();
    expect(queryOf(fetchMock.calls, SUMMARY_KEY).get('group_by')).toBe('model');

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText('Group by'), 'workflow');
    await waitFor(() =>
      expect(queryOf(fetchMock.calls, SUMMARY_KEY).get('group_by')).toBe('workflow'),
    );

    await user.click(screen.getByRole('button', { name: 'Show data table' }));
    const table = screen.getByRole('table', { name: 'Daily tokens' });
    const day1 = within(table).getByRole('row', { name: /2026-11-01/ });
    expect(within(day1).getByText('140')).toBeInTheDocument(); // summed per day
    expect(within(day1).getByText('60')).toBeInTheDocument();
  });

  it('offers every contract dimension', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse(SUMMARY), [EVENTS_KEY]: () => list([]) });
    renderWithClient(<UsageView />);
    const select = await screen.findByLabelText('Group by');
    expect(
      within(select)
        .getAllByRole('option')
        .map((o) => o.getAttribute('value')),
    ).toEqual([
      'workspace',
      'project',
      'user',
      'agent',
      'prompt',
      'model',
      'pipeline',
      'workflow',
      'run',
      'build',
    ]);
  });

  it('does not show money on the token page', async () => {
    stubGov({ [SUMMARY_KEY]: () => jsonResponse(SUMMARY), [EVENTS_KEY]: () => list([]) });
    renderWithClient(<UsageView />);
    await screen.findByText('Reconciled: consistent');
    expect(screen.queryByText(/Total known/)).not.toBeInTheDocument();
  });
});

describe('Model consumption', () => {
  it('groups by model with labelled cost columns', async () => {
    const fetchMock = stubGov({
      [SUMMARY_KEY]: () =>
        jsonResponse({
          ...SUMMARY,
          groups: [{ ...SUMMARY.groups[0], key: 'm1', label: 'fake-chat' }],
        }),
    });
    renderWithClient(<ModelConsumptionView />);
    expect(await screen.findByText('fake-chat')).toBeInTheDocument();
    expect(queryOf(fetchMock.calls, SUMMARY_KEY).get('group_by')).toBe('model');
    const table = screen.getByRole('table', { name: 'Consumption by model' });
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((h) => h.textContent),
    ).toContain('Calculated from rate card v2 (not an invoice)');
    expect(COST.unmeasured_count).toBe(3);
  });
});
