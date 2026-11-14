'use client';

import { useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ApiError } from '@/lib/api/errors';
import { keys, useAuthConfig } from '@/lib/hooks';
import { oidcLoginHref } from '@/lib/oidc';
import { ActionError, TextField, useAction } from './admin/ui';
import { ErrorMessage } from './ErrorMessage';

export const MIN_PASSWORD_LENGTH = 12;

export function validateNewPassword(password: string, confirm: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
  }
  if (password !== confirm) return 'The two passwords do not match.';
  return null;
}

/** Redeems a one-time setup token (`/setup?token=…`). */
export function SetupForm() {
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const config = useAuthConfig();
  const action = useAction();
  const token = params.get('token') ?? '';
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [pendingLink, setPendingLink] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const localEnabled = config.data?.local_enabled === true;
    if (localEnabled) {
      const err = validateNewPassword(password, confirm);
      setFieldError(err);
      if (err) return;
    }
    const res = await action.run(() =>
      endpoints.redeemSetup(token, localEnabled ? password : undefined),
    );
    setPassword('');
    setConfirm('');
    if (!res.ok) return;
    if ('csrf_token' in res.value) {
      queryClient.setQueryData(keys.session, res.value);
      router.replace('/');
    } else {
      setPendingLink(true);
    }
  }

  // An unusable token is reported generically; whether it expired or was used is not revealed.
  const rejected = action.error instanceof ApiError && action.error.status < 500;

  return (
    <div className="card w-full max-w-sm">
      <h1 className="text-xl font-semibold text-slate-900">Set up your account</h1>

      {token === '' && (
        <p
          role="alert"
          className="mt-4 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
        >
          This page needs a setup token. Use the link your administrator gave you.
        </p>
      )}
      {config.isPending && token !== '' && (
        <p role="status" className="mt-4 text-sm text-slate-600">
          Loading...
        </p>
      )}
      {config.isError && (
        <div className="mt-4">
          <ErrorMessage error={config.error} title="Could not load sign-in options" />
        </div>
      )}

      {token !== '' && config.data && !pendingLink && (
        <form onSubmit={(e) => void submit(e)} className="mt-4 space-y-4" noValidate>
          {config.data.local_enabled ? (
            <>
              <TextField
                label="New password"
                type="password"
                value={password}
                onChange={setPassword}
                autoComplete="new-password"
                help={`At least ${MIN_PASSWORD_LENGTH} characters.`}
              />
              <TextField
                label="Confirm password"
                type="password"
                value={confirm}
                onChange={setConfirm}
                autoComplete="new-password"
                error={fieldError}
              />
            </>
          ) : (
            <p className="text-sm text-slate-700">
              This installation uses an identity provider for sign-in. Continue to link your account
              to your setup token.
            </p>
          )}
          {rejected ? (
            <p
              role="alert"
              className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
            >
              This setup link is invalid or has expired. Ask your administrator for a new one.
            </p>
          ) : (
            <ActionError error={action.error} title="Could not complete setup" />
          )}
          <button type="submit" className="btn btn-primary w-full" disabled={action.busy}>
            {action.busy ? 'Working...' : config.data.local_enabled ? 'Set password' : 'Continue'}
          </button>
        </form>
      )}

      {pendingLink && config.data && (
        <div className="mt-4 space-y-3">
          <p role="status" className="text-sm text-slate-800">
            Your setup token has been accepted. Sign in with your identity provider to finish
            linking your account.
          </p>
          {config.data.oidc_providers.length === 0 ? (
            <p className="text-sm text-slate-700">
              No identity provider is available yet. Contact your administrator.
            </p>
          ) : (
            config.data.oidc_providers.map((p) => (
              <a key={p.id} href={oidcLoginHref(p.login_url, '/')} className="btn w-full">
                Sign in with {p.display_name}
              </a>
            ))
          )}
        </div>
      )}

      <p className="mt-4 text-center text-sm">
        <Link href="/login" className="text-blue-800 underline">
          Back to sign in
        </Link>
      </p>
    </div>
  );
}
