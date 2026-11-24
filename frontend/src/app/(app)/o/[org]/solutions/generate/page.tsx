import type { Metadata } from 'next';
import { Suspense } from 'react';
import { GenerateView } from '@/components/appgen/GenerateView';

export const metadata: Metadata = { title: 'Generate solution' };

export default function Page() {
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <GenerateView />
    </Suspense>
  );
}
