import { screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { stubApi } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { Sidebar } from './Sidebar';

let pathname = '/o/acme';
vi.mock('next/navigation', () => ({ usePathname: () => pathname }));

beforeEach(() => {
  pathname = '/o/acme';
});
afterEach(() => vi.unstubAllGlobals());

describe('Sidebar Security group', () => {
  it('lists only the Security areas the user may use', async () => {
    stubApi({}, ['member:read', 'audit:read']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(await screen.findByRole('link', { name: 'Users', hidden: true })).toHaveAttribute(
      'href',
      '/o/acme/security/users',
    );
    expect(screen.getByRole('link', { name: 'Security Audit', hidden: true })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Security' })).toBeInTheDocument();
    for (const hidden of ['Roles', 'Groups', 'Secrets', 'API Keys', 'Service Accounts']) {
      expect(screen.queryByRole('link', { name: hidden, hidden: true })).not.toBeInTheDocument();
    }
  });

  it('shows no Security group when the user has no security permissions', async () => {
    const fetchMock = stubApi({}, ['workspace:read']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === 'GET /api/v1/orgs/acme/me/permissions')).toBe(
        true,
      ),
    );
    expect(
      await screen.findByRole('link', { name: 'Dashboard', hidden: true }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Security' })).not.toBeInTheDocument();
  });

  it('keeps the group hidden when the permissions request fails (server stays authoritative)', async () => {
    stubApi({
      'GET /api/v1/orgs/acme/me/permissions': () => new Response('{}', { status: 500 }),
    });
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(
      await screen.findByRole('link', { name: 'Dashboard', hidden: true }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Security' })).not.toBeInTheDocument();
  });

  it('requests permissions at the current workspace and project scope', async () => {
    pathname = '/o/acme/w/eng/p/api';
    const fetchMock = stubApi({}, ['role:read']);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await screen.findByRole('link', { name: 'Roles', hidden: true });
    const call = fetchMock.calls.find((c) => c.key.endsWith('/me/permissions'));
    expect(call?.url).toContain('workspace=eng');
    expect(call?.url).toContain('project=api');
  });
});
