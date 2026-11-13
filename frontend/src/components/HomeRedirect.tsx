'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect } from 'react';
import { useSession } from '@/lib/hooks';
import { landingHref } from '@/lib/scope';
import { ErrorMessage } from './ErrorMessage';

/** `/` → first (active) org's landing page, or an honest empty state. */
export function HomeRedirect() {
  const router = useRouter();
  const session = useSession();
  const first = session.data?.memberships.find((m) => m.org.status === 'active');

  useEffect(() => {
    if (first) router.replace(landingHref(first.org.slug));
  }, [first, router]);

  if (session.isError) return <ErrorMessage error={session.error} />;
  if (session.isPending || first) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading...
      </p>
    );
  }
  return (
    <section className="mx-auto max-w-lg py-16 text-center" aria-labelledby="empty-title">
      <h1 id="empty-title" className="text-xl font-semibold text-slate-900">
        You are not a member of any organization
      </h1>
      <p className="mt-2 text-sm text-slate-600">
        Ask an administrator to add you to an organization.
        {session.data.user.is_platform_admin && ' As a platform administrator you can create one.'}
      </p>
      {session.data.user.is_platform_admin && (
        <p className="mt-4">
          <Link
            href="/platform/organizations"
            className="text-sm font-medium text-blue-800 underline"
          >
            Manage organizations
          </Link>
        </p>
      )}
    </section>
  );
}
