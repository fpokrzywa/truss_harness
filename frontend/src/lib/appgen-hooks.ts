'use client';

import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';
import type { RequestStatus } from './api/appgen-types';
import { endpoints } from './api/endpoints';

export const APP_REQUEST_POLL_MS = 3000;
/** Polling gives up this long after the screen started it; a refresh checks again. */
export const APP_REQUEST_POLL_LIMIT_MS = 10 * 60 * 1000;

const POLLING: ReadonlySet<string> = new Set<RequestStatus>(['queued', 'drafting', 'accepting']);

/** True while the worker still owns the request (queued, drafting or accepting). */
export const isWorking = (status: string | undefined) =>
  status !== undefined && POLLING.has(status);

/** The `refetchInterval` of one request: every 3 s while it is working, until the time limit. */
export const appRequestPollInterval = (
  status: string | undefined,
  timedOut: boolean,
): number | false => (!timedOut && isWorking(status) ? APP_REQUEST_POLL_MS : false);

const key = (org: string, ...rest: unknown[]) => ['appgen', org, ...rest] as const;

/** The project's app requests, newest first, paged through `next_cursor`. */
export const useAppRequests = (org: string, ws: string, proj: string, status: RequestStatus | '') =>
  useInfiniteQuery({
    queryKey: key(org, 'requests', ws, proj, status),
    queryFn: ({ pageParam }) =>
      endpoints.listAppRequests(org, ws, proj, {
        ...(status ? { status } : {}),
        ...(pageParam ? { cursor: pageParam } : {}),
      }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!ws && !!proj,
  });

/**
 * One app request, polled while it is working. Polling stops on any other status, and
 * `APP_REQUEST_POLL_LIMIT_MS` after it started (`timedOut`); it starts afresh when the request
 * starts working again. TanStack pauses it while the tab is hidden.
 */
export function useAppRequest(org: string, id: string) {
  const [timedOut, setTimedOut] = useState(false);
  const query = useQuery({
    queryKey: key(org, 'request', id),
    queryFn: () => endpoints.getAppRequest(org, id),
    refetchInterval: (q) => appRequestPollInterval(q.state.data?.status, timedOut),
    refetchIntervalInBackground: false,
  });
  const working = isWorking(query.data?.status);
  const [wasWorking, setWasWorking] = useState(working);
  if (working !== wasWorking) {
    setWasWorking(working);
    if (working) setTimedOut(false);
  }
  useEffect(() => {
    if (!working) return;
    const timer = setTimeout(() => setTimedOut(true), APP_REQUEST_POLL_LIMIT_MS);
    return () => clearTimeout(timer);
  }, [working]);
  return { query, polling: working && !timedOut, timedOut: working && timedOut };
}

/** Refetches every cached app request query of the organization. */
export function useRefreshAppRequests(org: string) {
  const qc = useQueryClient();
  return useCallback(() => qc.invalidateQueries({ queryKey: ['appgen', org] }), [qc, org]);
}
