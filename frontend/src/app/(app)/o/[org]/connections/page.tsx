import type { Metadata } from 'next';
import { Suspense } from 'react';
import { ConnectionsSpaceView } from '@/components/integrate/ConnectionsSpaceView';

export const metadata: Metadata = { title: 'Connections' };

export default function Page() {
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <ConnectionsSpaceView />
    </Suspense>
  );
}
