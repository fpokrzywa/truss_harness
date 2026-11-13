'use client';

import { ApiError } from '@/lib/api/errors';
import { ErrorMessage } from './ErrorMessage';
import { NoAccess } from './NoAccess';

/** 403/404 render the neutral no-access page; everything else shows the structured error. */
export function QueryError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.isNoAccess) return <NoAccess />;
  return <ErrorMessage error={error} />;
}
