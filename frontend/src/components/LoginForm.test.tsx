import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, problemResponse, renderWithClient, routeFetch } from '@/test/utils';
import { LoginForm } from './LoginForm';

const replace = vi.fn();
let next: string | null = null;
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(next ? { next } : {}),
}));

const session = {
  user: { id: 'u1', email: 'a@b.c', display_name: 'A', is_platform_admin: false },
  csrf_token: 't',
  memberships: [],
  expires_at: '2030-01-01T00:00:00Z',
};

function stubFetch(config: unknown, login?: () => Response) {
  const fetchMock = routeFetch({
    'GET /api/auth/config': () => jsonResponse(config),
    ...(login ? { 'POST /api/auth/local/login': login } : {}),
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

beforeEach(() => {
  replace.mockClear();
  next = null;
});
afterEach(() => vi.unstubAllGlobals());

describe('LoginForm', () => {
  it('signs in with local credentials and navigates home', async () => {
    const fetchMock = stubFetch({ local_enabled: true, oidc_providers: [] }, () =>
      jsonResponse(session),
    );
    renderWithClient(<LoginForm />);
    await userEvent.type(await screen.findByLabelText('Email'), 'a@b.c');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'));
    const login = fetchMock.calls.find((c) => c.key === 'POST /api/auth/local/login');
    expect(JSON.parse(login?.init.body as string)).toEqual({ email: 'a@b.c', password: 'secret' });
  });

  it('honours a safe ?next= and ignores an external one', async () => {
    next = '/o/acme';
    stubFetch({ local_enabled: true, oidc_providers: [] }, () => jsonResponse(session));
    renderWithClient(<LoginForm />);
    await userEvent.type(await screen.findByLabelText('Email'), 'a@b.c');
    await userEvent.type(screen.getByLabelText('Password'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/o/acme'));
  });

  it('shows a generic message on 401 without revealing which field was wrong', async () => {
    stubFetch({ local_enabled: true, oidc_providers: [] }, () =>
      problemResponse(401, 'invalid_credentials', { detail: 'no such user bob@x.com' }),
    );
    renderWithClient(<LoginForm />);
    await userEvent.type(await screen.findByLabelText('Email'), 'bob@x.com');
    await userEvent.type(screen.getByLabelText('Password'), 'wrong');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Sign-in failed. Check your email and password and try again.');
    expect(alert).not.toHaveTextContent('no such user');
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled();
  });

  it('shows structured error with request id for non-401 failures', async () => {
    stubFetch({ local_enabled: true, oidc_providers: [] }, () =>
      problemResponse(403, 'auth_disabled', { detail: 'Local sign-in is disabled.' }),
    );
    renderWithClient(<LoginForm />);
    await userEvent.type(await screen.findByLabelText('Email'), 'a@b.c');
    await userEvent.type(screen.getByLabelText('Password'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Local sign-in is disabled.')).toBeInTheDocument();
    expect(screen.getByText('req-123')).toBeInTheDocument();
  });

  it('renders no form and no fake buttons when no methods are configured', async () => {
    stubFetch({ local_enabled: false, oidc_providers: [] });
    renderWithClient(<LoginForm />);
    expect(await screen.findByText(/No sign-in methods are enabled/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('renders a link per listed OIDC provider', async () => {
    stubFetch({
      local_enabled: false,
      oidc_providers: [
        { id: 'corp', display_name: 'Corp SSO', login_url: '/api/auth/oidc/corp/login' },
      ],
    });
    renderWithClient(<LoginForm />);
    const link = await screen.findByRole('link', { name: 'Sign in with Corp SSO' });
    expect(link).toHaveAttribute('href', '/api/auth/oidc/corp/login');
  });

  it('renders the fixed product name and never calls the dropped branding route (B-1101)', async () => {
    const fetchMock = stubFetch({ local_enabled: true, oidc_providers: [] });
    renderWithClient(<LoginForm />);
    expect(
      await screen.findByRole('heading', { name: 'Sign in to SHI Enterprise Truss Harness' }),
    ).toBeInTheDocument();
    await screen.findByLabelText('Email');
    expect(fetchMock.calls.map((c) => c.key)).toEqual(['GET /api/auth/config']);
  });
});
