import type { Metadata } from 'next';
import { Suspense } from 'react';
import { SolutionDetailView } from '@/components/releases/SolutionDetailView';

export const metadata: Metadata = { title: 'Solution' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <SolutionDetailView id={id} />
    </Suspense>
  );
}
