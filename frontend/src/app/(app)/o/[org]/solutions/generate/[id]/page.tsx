import type { Metadata } from 'next';
import { Suspense } from 'react';
import { AppRequestView } from '@/components/appgen/AppRequestView';

export const metadata: Metadata = { title: 'App request' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <AppRequestView id={id} />
    </Suspense>
  );
}
