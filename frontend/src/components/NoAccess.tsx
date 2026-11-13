import Link from 'next/link';

/**
 * Deliberately identical for "does not exist" and "not allowed": never reveals which
 * (FR-TEN-006, no existence oracle).
 */
export function NoAccess() {
  return (
    <section className="mx-auto max-w-lg py-16 text-center" aria-labelledby="no-access-title">
      <h1 id="no-access-title" className="text-xl font-semibold text-slate-900">
        Not found or no access
      </h1>
      <p className="mt-2 text-sm text-slate-600">
        This page does not exist, or you do not have permission to view it.
      </p>
      <p className="mt-4">
        <Link href="/" className="text-sm font-medium text-blue-800 underline">
          Go to your dashboard
        </Link>
      </p>
    </section>
  );
}
