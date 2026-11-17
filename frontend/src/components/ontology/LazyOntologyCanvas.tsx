'use client';

import dynamic from 'next/dynamic';

// React Flow measures the DOM, so it is loaded on the client only.
export const LazyOntologyCanvas = dynamic(
  () => import('./OntologyCanvas').then((m) => m.OntologyCanvas),
  { ssr: false, loading: () => <p role="status">Loading canvas...</p> },
);
