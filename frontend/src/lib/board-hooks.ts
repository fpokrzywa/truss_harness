'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { endpoints } from './api/endpoints';

/** The board pages through `next_cursor`; empty filters mean the whole organization. */
export const useBoard = (org: string, filters: { workspace: string; project: string }) =>
  useInfiniteQuery({
    queryKey: ['board', org, filters.workspace, filters.project],
    queryFn: ({ pageParam }) =>
      endpoints.getBoard(org, { ...filters, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
