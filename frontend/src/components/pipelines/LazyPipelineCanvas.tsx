'use client';

import dynamic from 'next/dynamic';

// React Flow measures the DOM, so it is loaded on the client only.
export const LazyPipelineCanvas = dynamic(
  () => import('./PipelineCanvas').then((m) => m.PipelineCanvas),
  { ssr: false, loading: () => <p role="status">Loading canvas...</p> },
);
