import type { Metadata } from 'next';
import { Suspense } from 'react';
import { AssistSettingsView } from '@/components/assist/AssistSettingsView';

export const metadata: Metadata = { title: 'Assist settings' };

export default function Page() {
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <AssistSettingsView />
    </Suspense>
  );
}
