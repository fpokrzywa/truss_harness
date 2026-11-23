import type { Metadata } from 'next';
import { Suspense } from 'react';
import { BoardView } from '@/components/board/BoardView';

export const metadata: Metadata = { title: 'Board' };

export default function Page() {
  // useSearchParams (?ws=&proj=) needs a Suspense boundary for static rendering.
  return (
    <Suspense fallback={null}>
      <BoardView />
    </Suspense>
  );
}
