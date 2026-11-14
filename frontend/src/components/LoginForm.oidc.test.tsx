import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { oidcLoginHref } from '@/lib/oidc';
import { jsonResponse, renderWithClient, routeFetch } from '@/test/utils';
import { LoginForm } from './LoginForm';

let search = '';
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(search),
}));

const CONFIG = {
  local_enabled: false,
  oidc_providers: [
    { id: 'corp', display_name: 'Corp SSO', login_url: '/api/auth/oidc/corp/login' },
    { id: 'gh', display_name: 'Other IdP', login_url: '/api/auth/oidc/gh/login' },
  ],
};

beforeEach(() => {
  search = '';
  vi.stubGlobal('fetch', routeFetch({ 'GET /api/auth/config': () => jsonResponse(CONFIG) }));
});
afterEach(() => vi.unstubAllGlobals());

describe('OIDC sign-in buttons', () => {
  it('render one link per provider from /api/auth/config', async () => {
    renderWithClient(<LoginForm />);
    expect(await screen.findByRole('link', { name: 'Sign in with Corp SSO' })).toHaveAttribute(
      'href',
      '/api/auth/oidc/corp/login',
    );
    expect(screen.getByRole('link', { name: 'Sign in with Other IdP' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
  });

  it('carry a safe ?next= through to the login URL', async () => {
    search = 'next=%2Fo%2Facme%2Fsecurity%2Fusers';
    renderWithClient(<LoginForm />);
    expect(await screen.findByRole('link', { name: 'Sign in with Corp SSO' })).toHaveAttribute(
      'href',
      '/api/auth/oidc/corp/login?next=%2Fo%2Facme%2Fsecurity%2Fusers',
    );
  });

  it('drop an external or protocol-relative next', async () => {
    search = 'next=https%3A%2F%2Fevil.example%2Fx';
    renderWithClient(<LoginForm />);
    expect(await screen.findByRole('link', { name: 'Sign in with Corp SSO' })).toHaveAttribute(
      'href',
      '/api/auth/oidc/corp/login',
    );
  });

  it('show a generic message for ?error=oidc_failed', async () => {
    search = 'error=oidc_failed';
    renderWithClient(<LoginForm />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/did not complete/i);
    expect(alert).not.toHaveTextContent(/oidc_failed/);
  });
});

describe('oidcLoginHref', () => {
  it('appends with & when the URL already has a query', () => {
    expect(oidcLoginHref('/a?x=1', '/p?q=1')).toBe('/a?x=1&next=%2Fp%3Fq%3D1');
  });
  it('rejects backslash and protocol-relative targets', () => {
    expect(oidcLoginHref('/a', '//evil.example')).toBe('/a');
    expect(oidcLoginHref('/a', '/\\evil.example')).toBe('/a');
  });
});
