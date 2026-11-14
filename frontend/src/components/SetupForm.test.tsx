import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { bodyOf, SESSION } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient, routeFetch } from '@/test/utils';
import { SetupForm, validateNewPassword } from './SetupForm';

const replace = vi.fn();
let search = 'token=tok-123';
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(search),
}));

function stub(config: unknown, redeem?: () => Response) {
  const fetchMock = routeFetch({
    'GET /api/auth/config': () => jsonResponse(config),
    ...(redeem ? { 'POST /api/auth/setup/redeem': redeem } : {}),
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

beforeEach(() => {
  replace.mockClear();
  search = 'token=tok-123';
});
afterEach(() => vi.unstubAllGlobals());

describe('validateNewPassword', () => {
  it('requires 12 characters and a matching confirmation', () => {
    expect(validateNewPassword('short', 'short')).toMatch(/at least 12/);
    expect(validateNewPassword('a'.repeat(11), 'a'.repeat(11))).toMatch(/at least 12/);
    expect(validateNewPassword('a'.repeat(12), 'b'.repeat(12))).toMatch(/do not match/);
    expect(validateNewPassword('a'.repeat(12), 'a'.repeat(12))).toBeNull();
  });
});

describe('SetupForm', () => {
  it('blocks short or mismatched passwords without calling the API', async () => {
    const fetchMock = stub({ local_enabled: true, oidc_providers: [] });
    renderWithClient(<SetupForm />);
    await userEvent.type(await screen.findByLabelText('New password'), 'too-short');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'too-short');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    expect(await screen.findByText(/at least 12 characters\./, { selector: 'p' })).toBeVisible();
    await userEvent.clear(screen.getByLabelText('New password'));
    await userEvent.clear(screen.getByLabelText('Confirm password'));
    await userEvent.type(screen.getByLabelText('New password'), 'long-enough-password');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'different-password!');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    expect(await screen.findByText('The two passwords do not match.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('redeems the token with the password and goes home', async () => {
    const fetchMock = stub({ local_enabled: true, oidc_providers: [] }, () =>
      jsonResponse(SESSION),
    );
    renderWithClient(<SetupForm />);
    await userEvent.type(await screen.findByLabelText('New password'), 'long-enough-password');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'long-enough-password');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'));
    expect(bodyOf(fetchMock, 'POST /api/auth/setup/redeem')).toEqual({
      token: 'tok-123',
      password: 'long-enough-password',
    });
  });

  it('without local auth, redeems the token alone and tells the user to sign in with the IdP', async () => {
    const fetchMock = stub(
      {
        local_enabled: false,
        oidc_providers: [
          { id: 'corp', display_name: 'Corp SSO', login_url: '/api/auth/oidc/corp/login' },
        ],
      },
      () => jsonResponse({ status: 'pending_oidc_link' }),
    );
    renderWithClient(<SetupForm />);
    expect(screen.queryByLabelText('New password')).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: 'Continue' }));
    expect(await screen.findByText(/finish linking your account/i)).toBeInTheDocument();
    expect(bodyOf(fetchMock, 'POST /api/auth/setup/redeem')).toEqual({ token: 'tok-123' });
    expect(screen.getByRole('link', { name: 'Sign in with Corp SSO' })).toHaveAttribute(
      'href',
      '/api/auth/oidc/corp/login',
    );
    expect(replace).not.toHaveBeenCalled();
  });

  it('reports an unusable token generically', async () => {
    stub({ local_enabled: true, oidc_providers: [] }, () => problemResponse(400, 'invalid_token'));
    renderWithClient(<SetupForm />);
    await userEvent.type(await screen.findByLabelText('New password'), 'long-enough-password');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'long-enough-password');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    expect(await screen.findByText(/invalid or has expired/i)).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it('asks for a token when the URL has none', async () => {
    search = '';
    stub({ local_enabled: true, oidc_providers: [] });
    renderWithClient(<SetupForm />);
    expect(await screen.findByText(/needs a setup token/i)).toBeInTheDocument();
    expect(screen.queryByLabelText('New password')).not.toBeInTheDocument();
  });
});
