import type { Metadata } from 'next';
import { Suspense } from 'react';
import { DatasetsView } from '@/components/releases/DatasetsView';

export const metadata: Metadata = { title: 'Evaluation datasets' };

export default function Page() {
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <DatasetsView />
    </Suspense>
  );
}
