'use client';

import type { ReactNode } from 'react';
import { useSession } from '@/lib/hooks';
import { DEFAULT_FEATURES } from '@/lib/nav';

/**
 * Renders children only when the installation enables `feature` (session.features; tenancy only when
 * absent). Children are not mounted otherwise, so a direct URL into a disabled area makes no API calls.
 */
export function FeatureGate({ feature, children }: { feature: string; children: ReactNode }) {
  const session = useSession();
  if (session.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading...
      </p>
    );
  }
  const features = session.data?.features ?? DEFAULT_FEATURES;
  if (!features.includes(feature)) {
    return (
      <section className="mx-auto max-w-lg py-16 text-center" aria-labelledby="feature-off-title">
        <h1 id="feature-off-title" className="text-xl font-semibold text-slate-900">
          This area is not available in this installation
        </h1>
        <p className="mt-2 text-sm text-slate-600">
          The feature is not enabled for this deployment.
        </p>
      </section>
    );
  }
  return <>{children}</>;
}
