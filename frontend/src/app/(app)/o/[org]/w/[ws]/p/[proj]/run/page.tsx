import type { Metadata } from 'next';
import { Suspense } from 'react';
import { RunConsole } from '@/components/operate/RunConsole';

export const metadata: Metadata = { title: 'Agent Execution Console' };

export default function Page() {
  // useSearchParams (`?agent=` preselect from the agent page) needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <RunConsole />
    </Suspense>
  );
}
