import { screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, renderWithClient, routeFetch } from '@/test/utils';
import { HomeRedirect } from './HomeRedirect';

const replace = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace }) }));

const user = { id: 'u1', email: 'a@example.test', display_name: 'A', is_platform_admin: false };
const membership = (slug: string, status: string) => ({
  org: { id: slug, slug, name: slug, status },
  roles: [],
});
const stubSession = (memberships: unknown[]) =>
  vi.stubGlobal(
    'fetch',
    routeFetch({ 'GET /api/auth/session': () => jsonResponse({ user, memberships }) }),
  );

beforeEach(() => replace.mockClear());
afterEach(() => vi.unstubAllGlobals());

describe('HomeRedirect', () => {
  it('lands on the command center of the first active organization', async () => {
    stubSession([membership('old', 'suspended'), membership('demo a', 'active')]);
    renderWithClient(<HomeRedirect />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/o/demo%20a/command'));
  });

  it('does not redirect without an active organization', async () => {
    stubSession([]);
    renderWithClient(<HomeRedirect />);
    expect(
      await screen.findByRole('heading', { name: 'Your account has no organization yet' }),
    ).toBeVisible();
    expect(screen.getByText('Ask an administrator to add you to an organization.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Manage organizations' })).not.toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });
});
