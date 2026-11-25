import type { Metadata } from 'next';
import { Suspense } from 'react';
import { AskSethView } from '@/components/assist/AskSethView';

export const metadata: Metadata = { title: 'Ask SETH' };

export default function Page() {
  // useSearchParams (?context=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <AskSethView />
    </Suspense>
  );
}
