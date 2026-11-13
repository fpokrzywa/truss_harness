import { screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SESSION, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { FeatureGate } from '../FeatureGate';
import { Sidebar } from '../Sidebar';

vi.mock('next/navigation', () => ({ usePathname: () => '/o/acme' }));
afterEach(() => vi.unstubAllGlobals());

const session = (features: string[], admin = false) =>
  jsonResponse({
    ...SESSION,
    user: { ...SESSION.user, is_platform_admin: admin },
    features,
  });

describe('Govern feature gate', () => {
  it('does not mount governance pages or call their APIs when the feature is off', async () => {
    const fetchMock = stubApi({ 'GET /api/auth/session': () => session(['tenancy', 'build']) });
    renderWithClient(
      <FeatureGate feature="governance">
        <p>cost explorer</p>
      </FeatureGate>,
    );
    expect(
      await screen.findByRole('heading', {
        name: 'This area is not available in this installation',
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText('cost explorer')).not.toBeInTheDocument();
    expect(fetchMock.calls.every((c) => c.key === 'GET /api/auth/session')).toBe(true);
  });

  it('renders governance pages when the feature is on', async () => {
    stubApi({ 'GET /api/auth/session': () => session(['tenancy', 'governance']) });
    renderWithClient(
      <FeatureGate feature="governance">
        <p>cost explorer</p>
      </FeatureGate>,
    );
    expect(await screen.findByText('cost explorer')).toBeInTheDocument();
  });
});

describe('Govern navigation gating', () => {
  const links = () => {
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    return within(nav)
      .queryAllByRole('link', { hidden: true })
      .map((l) => l.textContent);
  };

  it('hides the Govern group without the feature, whatever the permissions', async () => {
    stubApi({ 'GET /api/auth/session': () => session(['tenancy']) }, [
      'usage:read',
      'budget:read',
      'governance:read',
      'pricing:read',
      'audit:read',
      'ops:read',
    ]);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await screen.findByRole('link', { name: 'Workspaces', hidden: true });
    expect(screen.queryByRole('heading', { name: 'Govern' })).not.toBeInTheDocument();
  });

  it('shows only the items each permission allows', async () => {
    stubApi({ 'GET /api/auth/session': () => session(['tenancy', 'governance']) }, [
      'usage:read',
      'pricing:read',
    ]);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(await screen.findByRole('link', { name: 'Token Usage', hidden: true })).toHaveAttribute(
      'href',
      '/o/acme/govern/usage',
    );
    expect(links()).toEqual(
      expect.arrayContaining(['Token Usage', 'Cost Explorer', 'Model Consumption', 'Pricing']),
    );
    for (const hidden of [
      'Governance Overview',
      'Budgets & Quotas',
      'Execution Performance',
      'Audit Explorer',
      'Retention',
      'Policy Decisions',
      'Governance Policies',
    ])
      expect(links()).not.toContain(hidden);
  });

  it('shows Audit Explorer for audit:read and Global Rate Cards to platform admins only', async () => {
    stubApi({ 'GET /api/auth/session': () => session(['tenancy', 'governance'], true) }, [
      'audit:read',
    ]);
    renderWithClient(<Sidebar isPlatformAdmin />);
    expect(
      await screen.findByRole('link', { name: 'Audit Explorer', hidden: true }),
    ).toHaveAttribute('href', '/o/acme/govern/audit');
    expect(screen.getByRole('link', { name: 'Global Rate Cards', hidden: true })).toHaveAttribute(
      'href',
      '/platform/rate-cards',
    );
  });
});
