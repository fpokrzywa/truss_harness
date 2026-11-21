import type { Metadata } from 'next';
import { Suspense } from 'react';
import { SolutionsView } from '@/components/releases/SolutionsView';

export const metadata: Metadata = { title: 'Solutions' };

export default function Page() {
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <SolutionsView />
    </Suspense>
  );
}
