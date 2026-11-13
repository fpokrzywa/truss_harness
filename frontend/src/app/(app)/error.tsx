'use client';

import { ErrorMessage } from '@/components/ErrorMessage';

export default function AppError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto max-w-xl space-y-4">
      <ErrorMessage error={error} title="This page failed to load" />
      <button type="button" className="btn" onClick={reset}>
        Try again
      </button>
    </div>
  );
}
