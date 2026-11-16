import type { Metadata } from 'next';
import { Suspense } from 'react';
import { ModelsView } from '@/components/integrate/ModelsView';

export const metadata: Metadata = { title: 'Models' };

export default function Page() {
  // useSearchParams (register prefill) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <ModelsView />
    </Suspense>
  );
}
