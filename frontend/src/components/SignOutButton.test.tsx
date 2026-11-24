import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, problemResponse, renderWithClient, routeFetch } from '@/test/utils';
import { SignOutButton } from './SignOutButton';

const replace = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace }) }));

const session = () =>
  jsonResponse({
    user: { id: 'u1', email: 'a@example.test', display_name: 'A', is_platform_admin: false },
    memberships: [],
    csrf_token: 'csrf-1',
  });

beforeEach(() => replace.mockClear());
afterEach(() => vi.unstubAllGlobals());

describe('SignOutButton', () => {
  it('signs out and goes to /login', async () => {
    const fetch = routeFetch({
      'GET /api/auth/session': session,
      'POST /api/auth/logout': () => new Response(null, { status: 204 }),
    });
    vi.stubGlobal('fetch', fetch);
    renderWithClient(<SignOutButton />);
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/login'));
    expect(fetch.calls.map((c) => c.key)).toContain('POST /api/auth/logout');
  });

  it('shows the error and stays when sign out fails', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch({
        'GET /api/auth/session': session,
        'POST /api/auth/logout': () => problemResponse(500, 'internal_error'),
      }),
    );
    renderWithClient(<SignOutButton />);
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(await screen.findByText('Could not sign out')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeEnabled();
    expect(replace).not.toHaveBeenCalled();
  });
});
