import type { Metadata } from 'next';
import { Suspense } from 'react';
import { RunDetailView } from '@/components/operate/RunDetailView';

export const metadata: Metadata = { title: 'Run detail' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // useSearchParams (?tab=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <RunDetailView id={id} />
    </Suspense>
  );
}
