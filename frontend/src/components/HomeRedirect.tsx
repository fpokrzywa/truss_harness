'use client';

import { useRouter } from 'next/navigation';
import { useEffect } from 'react';
import { ApiError } from '@/lib/api/errors';
import { useSession } from '@/lib/hooks';
import { landingHref } from '@/lib/scope';
import { ErrorMessage } from './ErrorMessage';
import { SignOutButton } from './SignOutButton';

/**
 * `/` → `/login` when signed out, the first active org's landing page, the
 * platform organizations screen for a platform admin without one, or an
 * honest empty state.
 */
export function HomeRedirect() {
  const router = useRouter();
  const session = useSession();
  const unauthenticated = session.error instanceof ApiError && session.error.status === 401;
  const first = session.data?.memberships.find((m) => m.org.status === 'active');
  const target = unauthenticated
    ? '/login'
    : first
      ? landingHref(first.org.slug)
      : session.data?.user.is_platform_admin
        ? '/platform/organizations'
        : undefined;

  useEffect(() => {
    if (target) router.replace(target);
  }, [target, router]);

  if (unauthenticated) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Redirecting to sign in...
      </p>
    );
  }
  if (session.isError) return <ErrorMessage error={session.error} />;
  if (session.isPending || target) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading...
      </p>
    );
  }
  return (
    <section className="mx-auto max-w-lg py-16 text-center" aria-labelledby="empty-title">
      <h1 id="empty-title" className="text-xl font-semibold text-slate-900">
        Your account has no organization yet
      </h1>
      <p className="mt-2 text-sm text-slate-600">
        Ask an administrator to add you to an organization.
      </p>
      <div className="mt-4">
        <SignOutButton />
      </div>
    </section>
  );
}
