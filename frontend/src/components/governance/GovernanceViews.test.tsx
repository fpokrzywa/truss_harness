import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { COST, GOV_PERMS, queryOf, stubGov } from '@/test/governance';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import {
  DecisionsView,
  GovernanceOverviewView,
  PerformanceView,
  RetentionView,
} from './GovernanceViews';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const G = '/api/v1/orgs/acme/governance';

describe('GovernanceOverviewView', () => {
  it('shows budgets, labelled top spender costs, denials, approvals and policy counts', async () => {
    stubGov({
      [`GET ${G}/overview`]: () =>
        jsonResponse({
          budgets: [
            { id: 'b1', label: 'Org monthly', utilization: 0.9, alert_thresholds: [0.5, 0.8, 1] },
          ],
          top_spenders: [
            {
              key: 'p1',
              label: 'API project',
              cost_total_known: 7.5,
              cost: { ...COST, total_known: 4 },
              currency: 'USD',
            },
          ],
          denials_7d: 4,
          pending_approvals: 2,
          policy_counts: { approval_rule: 3, cost_threshold: 1 },
        }),
    });
    renderWithClient(<GovernanceOverviewView />);
    expect(
      await screen.findByRole('meter', { name: 'Utilization of Org monthly' }),
    ).toHaveAttribute('aria-valuenow', '90');
    const spenders = screen.getByRole('region', { name: 'Top spenders' });
    expect(within(spenders).getByText('API project')).toBeInTheDocument();
    expect(within(spenders).getByText('Provider-reported')).toBeInTheDocument();
    expect(within(spenders).getByText('Total known')).toBeInTheDocument();
    // G1 cost_total_known wins over the breakdown's own total.
    expect(within(spenders).getByText(/7\.50/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '2' })).toHaveAttribute('href', '/o/acme/approvals');
    expect(screen.getByText('4')).toBeInTheDocument();
    const counts = screen.getByRole('region', { name: 'Policy counts' });
    expect(within(counts).getByText('Approval rule')).toBeInTheDocument();
    expect(within(counts).getByText('3')).toBeInTheDocument();
  });
});

const retentionBody = (items: unknown[]) => ({
  items,
  enforcement: 'not_implemented',
  note: 'Retention settings are stored and audited; no job deletes data yet.',
});

describe('RetentionView', () => {
  const rows = [
    { data_class: 'audit_events', retain_days: 365, legal_hold: false },
    { data_class: 'usage_events', retain_days: 30, legal_hold: true, floor_days: 7 },
  ];

  it('previews purge counts without any delete control', async () => {
    const fetchMock = stubGov({
      [`GET ${G}/retention`]: () => jsonResponse(retentionBody(rows)),
      [`POST ${G}/retention/preview`]: () =>
        jsonResponse({
          items: [
            {
              data_class: 'usage_events',
              count: 1234,
              cutoff: '2026-10-01T00:00:00Z',
              on_legal_hold: true,
            },
          ],
          enforcement: 'not_implemented',
          note: 'Counts only.',
        }),
    });
    renderWithClient(<RetentionView />);
    const user = userEvent.setup();
    await screen.findByLabelText('Audit events retain days');
    // Honesty: the page states that retention is not enforced.
    expect(screen.getByRole('note')).toHaveTextContent('Retention is not enforced yet');
    expect(screen.getByRole('note')).toHaveTextContent('no job deletes data yet');
    await user.click(screen.getByRole('button', { name: 'Preview records older than the cutoff' }));
    const preview = await screen.findByRole('region', { name: 'Purge preview' });
    expect(within(preview).getByText(/Usage events: 1,234 \(on legal hold\)/)).toBeInTheDocument();
    expect(within(preview).getByText(/Retention is not enforced yet/)).toBeInTheDocument();
    expect(within(preview).getByText(/Nothing is deleted by this page/)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /delete|purge now|remove/i }),
    ).not.toBeInTheDocument();
    // The preview call is a POST to the preview route only; no DELETE or purge route was called.
    expect(fetchMock.calls.every((c) => !c.key.startsWith('DELETE'))).toBe(true);
    expect(bodyOf(fetchMock, `POST ${G}/retention/preview`)).toEqual({
      items: [
        { data_class: 'audit_events', retain_days: 365, legal_hold: false },
        { data_class: 'usage_events', retain_days: 30, legal_hold: true },
      ],
    });
  });

  it('shows platform floors and blocks values below them', async () => {
    stubGov({ [`GET ${G}/retention`]: () => jsonResponse(retentionBody(rows)) });
    renderWithClient(<RetentionView />);
    const user = userEvent.setup();
    const audit = await screen.findByLabelText('Audit events retain days');
    expect(screen.getByText('At least 90 days')).toBeInTheDocument(); // contract floor for audit
    expect(screen.getByText('At least 7 days')).toBeInTheDocument(); // server-provided floor
    await user.clear(audit);
    await user.type(audit, '30');
    expect(screen.getByText('Cannot be below the platform floor of 90 days.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save retention settings' })).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Preview records older than the cutoff' }),
    ).toBeDisabled();
  });

  it('saves edits and legal hold with PUT', async () => {
    const fetchMock = stubGov({
      [`GET ${G}/retention`]: () => jsonResponse(retentionBody(rows)),
      [`PUT ${G}/retention`]: () => jsonResponse(retentionBody(rows)),
    });
    renderWithClient(<RetentionView />);
    const user = userEvent.setup();
    const days = await screen.findByLabelText('Usage events retain days');
    await user.clear(days);
    await user.type(days, '45');
    await user.click(screen.getByLabelText('Legal hold on Audit events'));
    await user.click(screen.getByRole('button', { name: 'Save retention settings' }));
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === `PUT ${G}/retention`)).toBe(true),
    );
    expect(bodyOf(fetchMock, `PUT ${G}/retention`)).toEqual({
      items: [
        { data_class: 'audit_events', retain_days: 365, legal_hold: true },
        { data_class: 'usage_events', retain_days: 45, legal_hold: true },
      ],
    });
    expect(await screen.findByText('Retention settings saved.')).toBeInTheDocument();
  });

  it('is read-only without governance:manage', async () => {
    stubGov(
      { [`GET ${G}/retention`]: () => jsonResponse(retentionBody(rows)) },
      GOV_PERMS.filter((p) => p !== 'governance:manage'),
    );
    renderWithClient(<RetentionView />);
    expect(await screen.findByLabelText('Audit events retain days')).toBeDisabled();
    expect(
      screen.queryByRole('button', { name: 'Save retention settings' }),
    ).not.toBeInTheDocument();
  });
});

describe('DecisionsView', () => {
  it('lists decisions, filters by run and pages with the cursor', async () => {
    const fetchMock = stubGov({
      [`GET ${G}/decisions`]: ({ url }) =>
        new URL(url, 'http://x').searchParams.get('cursor') === 'c1'
          ? list([{ id: 'd2', decision: 'allow', reason_codes: ['within_budget'] }])
          : list(
              [
                {
                  id: 'd1',
                  decision: 'deny',
                  action: 'run.start',
                  reason_codes: ['budget_denied', 'quota_exceeded'],
                  reason_code: 'budget_denied',
                  policy_ids: ['pol-1'],
                  run_id: 'run-9',
                  at: '2026-11-03T10:00:00Z',
                },
              ],
              'c1',
            ),
    });
    renderWithClient(<DecisionsView />);
    const user = userEvent.setup();
    expect(await screen.findByText('budget_denied, quota_exceeded')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'run-9' })).toHaveAttribute(
      'href',
      '/o/acme/runs/run-9',
    );
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('within_budget')).toBeInTheDocument();
    await user.type(screen.getByLabelText('Run id'), 'run-9');
    await waitFor(() =>
      expect(queryOf(fetchMock.calls, `GET ${G}/decisions`).get('run_id')).toBe('run-9'),
    );
  });
});

describe('PerformanceView', () => {
  it('shows percentiles and error rates by agent and re-queries by model', async () => {
    const fetchMock = stubGov({
      'GET /api/v1/orgs/acme/observability/performance': ({ url }) =>
        new URL(url, 'http://x').searchParams.get('group_by') === 'model'
          ? jsonResponse({
              items: [
                {
                  key: 'm1',
                  label: 'fake-chat',
                  count: 5,
                  error_rate: 0,
                  p50_ms: 100,
                  p95_ms: 200,
                  p99_ms: 300,
                },
              ],
            })
          : jsonResponse({
              items: [
                {
                  key: 'a1',
                  label: 'Support agent',
                  count: 40,
                  error_count: 2,
                  error_rate: 0.05,
                  p50_ms: 850,
                  p95_ms: 2400,
                  p99_ms: 61000,
                },
              ],
            }),
    });
    renderWithClient(<PerformanceView />);
    const table = await screen.findByRole('table', { name: 'Latency and error rate by agent' });
    const row = within(table).getByRole('row', { name: /Support agent/ });
    expect(within(row).getByText('5.0%')).toBeInTheDocument();
    expect(within(row).getByText('2 errors')).toBeInTheDocument();
    expect(within(row).getByText('850 ms')).toBeInTheDocument();
    expect(within(row).getByText('2.40 s')).toBeInTheDocument();
    expect(within(row).getByText('1.0 min')).toBeInTheDocument();
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((h) => h.textContent),
    ).toEqual(['agent', 'Executions', 'Error rate', 'p50 latency', 'p95 latency', 'p99 latency']);

    await userEvent.setup().selectOptions(screen.getByLabelText('Group by'), 'model');
    expect(await screen.findByText('fake-chat')).toBeInTheDocument();
    expect(
      queryOf(fetchMock.calls, 'GET /api/v1/orgs/acme/observability/performance').get('group_by'),
    ).toBe('model');
  });
});
