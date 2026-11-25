import { screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { D, PLAN, PLAN_DIGEST, stubDelivery } from '@/test/delivery';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { PlanDetailView } from './PlanDetailView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

describe('PlanDetailView', () => {
  it('shows a sealed plan with its full digest, steps and a live countdown', async () => {
    stubDelivery();
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByText(PLAN_DIGEST)).toBeInTheDocument();
    expect(screen.getByRole('timer')).toHaveTextContent(/left\)$/);
    const steps = screen.getByRole('table', { name: 'Plan steps' });
    const row = within(steps).getByText('app.create').closest('tr')!;
    expect(within(row).getByText('create')).toBeInTheDocument();
    expect(within(row).getByText('apps/sales-app')).toBeInTheDocument();
    expect(within(row).getByText('Yes')).toBeInTheDocument();
    expect(screen.getByText('sales-app')).toBeInTheDocument();
    expect(screen.getByText('databricks-native/0.1.0')).toBeInTheDocument();
    expect(await screen.findByText('Analytics prod')).toBeInTheDocument();
  });

  it('shows Expired for a sealed plan whose expiry has passed', async () => {
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, expires_at: '2020-01-01T00:00:00Z' }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByRole('timer')).toHaveTextContent('(Expired)');
  });

  it('shows Not sealed yet and an empty steps state before planning finishes', async () => {
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () =>
        jsonResponse({ ...PLAN, status: 'planning', digest: null, steps: null }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByText('Not sealed yet')).toBeInTheDocument();
    expect(screen.getByText('No steps planned yet.')).toBeInTheDocument();
    expect(screen.queryByRole('timer')).not.toBeInTheDocument();
  });

  it.each([
    ['plan_failed', 'plan job failed: snowflake_patch_limit', 1],
    ['plan_failed', 'plan job failed: execution_failed', 0],
    ['plan_failed', 'snowflake_patch_limit', 0],
    ['rejected', 'plan job failed: snowflake_patch_limit', 0],
  ])(
    'shows the raw reason of a %s plan (%s) with %i patch limit hints',
    async (status, status_reason, hints) => {
      stubDelivery({
        [`GET ${D}/plans/p-1`]: () => jsonResponse({ ...PLAN, status, status_reason }),
      });
      renderWithClient(<PlanDetailView id="p-1" />);
      expect(await screen.findByText(status_reason)).toBeInTheDocument();
      expect(
        screen.queryAllByText(
          'This package has no free patch. Register a target with a new package and request the plan again.',
        ),
      ).toHaveLength(hints);
      expect(screen.queryAllByText(/no free patch/)).toHaveLength(hints);
    },
  );

  it('shows the decision and links to the deployment', async () => {
    stubDelivery({
      [`GET ${D}/plans/p-1`]: () =>
        jsonResponse({
          ...PLAN,
          status: 'approved',
          approval: {
            decision: 'approve',
            decided_by: 'u-other',
            decided_at: '2026-11-18T10:01:00Z',
            note: 'Looks right',
          },
          deployment_id: 'dep-1',
        }),
    });
    renderWithClient(<PlanDetailView id="p-1" />);
    expect(await screen.findByRole('link', { name: 'dep-1' })).toHaveAttribute(
      'href',
      '/o/acme/deliver/deployments/dep-1',
    );
    expect(screen.getByText('Approved')).toBeInTheDocument();
    expect(screen.getByText('Looks right')).toBeInTheDocument();
  });
});
