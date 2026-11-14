'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useRouter, useSearchParams } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import { keys, useAuthConfig } from '@/lib/hooks';
import { oidcLoginHref } from '@/lib/oidc';
import { DEFAULT_PRODUCT_NAME } from '@/lib/product';
import { safeNextPath } from '@/lib/validation';
import { ErrorMessage } from './ErrorMessage';

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const config = useAuthConfig();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setFailure(null);
    try {
      const session = await endpoints.login(email.trim(), password);
      queryClient.setQueryData(keys.session, session);
      router.replace(safeNextPath(params.get('next')));
    } catch (err) {
      setFailure(err);
      setBusy(false);
    }
  }

  const invalidCredentials = failure instanceof ApiError && failure.status === 401;

  return (
    <div className="card w-full max-w-sm">
      <h1 className="text-xl font-semibold text-slate-900">Sign in to {DEFAULT_PRODUCT_NAME}</h1>

      {params.get('error') === 'oidc_failed' && (
        // Deliberately generic: failure detail is only in the server audit log.
        <p
          role="alert"
          className="mt-4 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
        >
          Sign-in with your identity provider did not complete. Try again or contact your
          administrator.
        </p>
      )}

      {config.isPending && (
        <p role="status" className="mt-4 text-sm text-slate-600">
          Loading sign-in options...
        </p>
      )}
      {config.isError && (
        <div className="mt-4">
          <ErrorMessage error={config.error} title="Could not load sign-in options" />
        </div>
      )}

      {config.data && (
        <>
          {config.data.local_enabled && (
            <form onSubmit={(e) => void onSubmit(e)} className="mt-4 space-y-4" noValidate>
              <div>
                <label htmlFor="login-email" className="label">
                  Email
                </label>
                <input
                  id="login-email"
                  name="email"
                  type="email"
                  autoComplete="username"
                  required
                  className="input"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="login-password" className="label">
                  Password
                </label>
                <input
                  id="login-password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  className="input"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
              {invalidCredentials ? (
                <p
                  role="alert"
                  className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
                >
                  Sign-in failed. Check your email and password and try again.
                </p>
              ) : (
                failure !== null && <ErrorMessage error={failure} title="Could not sign in" />
              )}
              <button
                type="submit"
                className="btn btn-primary w-full"
                disabled={busy || email.trim() === '' || password === ''}
              >
                {busy ? 'Signing in...' : 'Sign in'}
              </button>
            </form>
          )}

          {config.data.oidc_providers.length > 0 && (
            <div className="mt-4 space-y-2">
              {config.data.local_enabled && (
                <p className="text-center text-xs text-slate-500">or continue with</p>
              )}
              {config.data.oidc_providers.map((p) => (
                // Full-page navigation to the API's OIDC login endpoint, not a client route.
                <a
                  key={p.id}
                  href={oidcLoginHref(p.login_url, params.get('next'))}
                  className="btn w-full"
                >
                  Sign in with {p.display_name}
                </a>
              ))}
            </div>
          )}

          {!config.data.local_enabled && config.data.oidc_providers.length === 0 && (
            <p className="mt-4 text-sm text-slate-700">
              No sign-in methods are enabled for this installation. Contact your administrator.
            </p>
          )}
        </>
      )}
    </div>
  );
}
