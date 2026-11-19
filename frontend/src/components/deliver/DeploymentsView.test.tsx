import { screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { D, DEPLOYMENT, list, stubDelivery } from '@/test/delivery';
import { renderWithClient } from '@/test/utils';
import { DeploymentsView } from './DeploymentsView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

describe('DeploymentsView', () => {
  it('lists deployments linking to their detail pages', async () => {
    stubDelivery({
      [`GET ${D}/deployments`]: () =>
        list([
          DEPLOYMENT,
          {
            ...DEPLOYMENT,
            id: 'dep-2',
            application_key: 'ops-app',
            status: 'unknown_outcome',
            status_reason: 'apply lease lost',
          },
        ]),
    });
    renderWithClient(<DeploymentsView />);
    const table = await screen.findByRole('table', { name: 'Deployments' });
    expect(within(table).getByRole('link', { name: 'sales-app' })).toHaveAttribute(
      'href',
      '/o/acme/deliver/deployments/dep-1',
    );
    const unknown = within(table).getByRole('link', { name: 'ops-app' });
    expect(unknown).toHaveAttribute('href', '/o/acme/deliver/deployments/dep-2');
    const row = unknown.closest('tr')!;
    expect(within(row).getByText('Outcome unknown')).toBeInTheDocument();
    expect(within(row).getByText('apply lease lost')).toBeInTheDocument();
    expect(within(row).queryByText('verified')).not.toBeInTheDocument();
    expect(await within(row).findByText('Analytics prod')).toBeInTheDocument();
  });

  it('shows an empty state', async () => {
    stubDelivery({ [`GET ${D}/deployments`]: () => list([]) });
    renderWithClient(<DeploymentsView />);
    expect(await screen.findByText('No deployments yet.')).toBeInTheDocument();
  });
});
