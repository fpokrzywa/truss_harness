'use client';

import { useParams } from 'next/navigation';

/** Org slug from the `/o/[org]/…` route. */
export function useOrgParam(): string {
  return decodeURIComponent(useParams<{ org: string }>().org);
}
