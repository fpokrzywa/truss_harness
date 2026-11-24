import { screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, problemResponse, renderWithClient, routeFetch } from '@/test/utils';
import HomePage from './page';

const replace = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace }) }));

const user = (is_platform_admin: boolean) => ({
  id: 'u1',
  email: 'a@example.test',
  display_name: 'A',
  is_platform_admin,
});
const membership = (slug: string, status: string) => ({
  org: { id: slug, slug, name: slug, status },
  roles: [],
});
const stubSession = (respond: () => Response) =>
  vi.stubGlobal('fetch', routeFetch({ 'GET /api/auth/session': respond }));

beforeEach(() => replace.mockClear());
afterEach(() => vi.unstubAllGlobals());

describe('HomePage', () => {
  it('sends a signed-out visitor to /login', async () => {
    stubSession(() => problemResponse(401, 'unauthenticated'));
    renderWithClient(<HomePage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/login'));
  });

  it('lands on the command center of the first active organization', async () => {
    stubSession(() =>
      jsonResponse({
        user: user(false),
        memberships: [membership('old', 'suspended'), membership('demo-a', 'active')],
      }),
    );
    renderWithClient(<HomePage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/o/demo-a/command'));
    expect(replace).toHaveBeenCalledTimes(1);
  });

  it('sends a platform admin without an organization to the organizations screen', async () => {
    stubSession(() => jsonResponse({ user: user(true), memberships: [] }));
    renderWithClient(<HomePage />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/platform/organizations'));
  });

  it('shows a no-organization page with sign out to anyone else', async () => {
    stubSession(() =>
      jsonResponse({ user: user(false), memberships: [membership('old', 'suspended')] }),
    );
    renderWithClient(<HomePage />);
    expect(
      await screen.findByRole('heading', { name: 'Your account has no organization yet' }),
    ).toBeVisible();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeVisible();
    expect(document.body).not.toHaveTextContent(/shell is running/);
    expect(replace).not.toHaveBeenCalled();
  });
});
