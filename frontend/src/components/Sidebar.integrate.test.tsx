import { screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubApi } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { Sidebar } from './Sidebar';

vi.mock('next/navigation', () => ({ usePathname: () => '/o/acme' }));
afterEach(() => vi.unstubAllGlobals());

describe('Sidebar Integrate group', () => {
  it('lists only the Integrate areas the user may read', async () => {
    stubApi({}, ['model:read', 'egress:manage']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(await screen.findByRole('link', { name: 'Models', hidden: true })).toHaveAttribute(
      'href',
      '/o/acme/integrate/models',
    );
    expect(screen.getByRole('link', { name: 'Model Providers', hidden: true })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Egress Policies', hidden: true })).toHaveAttribute(
      'href',
      '/o/acme/integrate/egress',
    );
    expect(screen.getByRole('heading', { name: 'Integrate' })).toBeInTheDocument();
    for (const hidden of ['Vendors', 'Connections', 'Adapters', 'Global Vendors']) {
      expect(screen.queryByRole('link', { name: hidden, hidden: true })).not.toBeInTheDocument();
    }
  });

  it('shows vendors, adapters and connections to readers', async () => {
    stubApi({}, ['vendor:read', 'connection:read']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(await screen.findByRole('link', { name: 'Vendors', hidden: true })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Adapters', hidden: true })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Connections', hidden: true })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Models', hidden: true })).not.toBeInTheDocument();
  });

  it('hides the group entirely without integrate permissions, and for platform links to non-admins', async () => {
    const fetchMock = stubApi({}, ['member:read']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key.endsWith('/me/permissions'))).toBe(true),
    );
    await screen.findByRole('link', { name: 'Users', hidden: true });
    expect(screen.queryByRole('heading', { name: 'Integrate' })).not.toBeInTheDocument();
  });

  it('shows platform admins the global vendor and default egress pages', async () => {
    stubApi({}, ['member:read']);
    renderWithClient(<Sidebar isPlatformAdmin />);
    expect(
      await screen.findByRole('link', { name: 'Global Vendors', hidden: true }),
    ).toHaveAttribute('href', '/platform/vendors');
    expect(screen.getByRole('link', { name: 'Default Egress', hidden: true })).toHaveAttribute(
      'href',
      '/platform/egress',
    );
  });
});
