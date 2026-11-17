'use client';

import { keepPreviousData, useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { endpoints } from './api/endpoints';
import type { Run, RunFilters } from './api/operate-types';
import { isTerminal, runFilterParams } from './run-utils';

export const POLL_MS = 1000;

const key = (org: string, ...rest: unknown[]) => ['operate', org, ...rest] as const;

/**
 * Polls every second while the run is non-terminal. TanStack pauses interval refetching while the
 * tab is hidden (refetchIntervalInBackground defaults to false), and the function form returns
 * false once the status is terminal so polling stops.
 */
const pollWhileActive = (run: Run | undefined): number | false =>
  run && isTerminal(run.status) ? false : POLL_MS;

export const useRun = (org: string, id: string | undefined) =>
  useQuery({
    queryKey: key(org, 'run', id),
    queryFn: () => endpoints.getRun(org, id as string),
    enabled: !!id,
    refetchInterval: (q) => pollWhileActive(q.state.data),
    refetchIntervalInBackground: false,
  });

/** Steps and events follow the run: poll while it is active; the status in the key forces one final fetch at terminal. */
export const useRunSteps = (org: string, id: string, run: Run | undefined) =>
  useQuery({
    queryKey: key(org, 'run', id, 'steps', run?.status),
    queryFn: () => endpoints.runSteps(org, id),
    enabled: !!run,
    refetchInterval: pollWhileActive(run),
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });

export const useRunEvents = (org: string, id: string, run: Run | undefined) =>
  useQuery({
    queryKey: key(org, 'run', id, 'events', run?.status),
    queryFn: () => endpoints.runEvents(org, id),
    enabled: !!run,
    refetchInterval: pollWhileActive(run),
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });

export const useRunResult = (org: string, id: string, enabled: boolean) =>
  useQuery({
    queryKey: key(org, 'run', id, 'result'),
    queryFn: () => endpoints.runResult(org, id),
    enabled,
    retry: false,
  });

export const useRuns = (org: string, filters: RunFilters) =>
  useInfiniteQuery({
    queryKey: key(org, 'runs', runFilterParams(filters)),
    queryFn: ({ pageParam }) => endpoints.listRuns(org, runFilterParams(filters), pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const useApprovals = (org: string, status: string) =>
  useInfiniteQuery({
    queryKey: key(org, 'approvals', status),
    queryFn: ({ pageParam }) => endpoints.listApprovals(org, status, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

/** Pending approvals of a run and every run below it: `outcome_unknown` on a parent can come from a child. */
export const useRunApprovals = (org: string, runId: string, enabled: boolean) =>
  useQuery({
    queryKey: key(org, 'approvals', 'pending', 'run', runId),
    queryFn: () => endpoints.listApprovals(org, 'pending', undefined, runId),
    enabled,
  });

export const useApproval = (org: string, id: string | undefined) =>
  useQuery({
    queryKey: key(org, 'approval', id),
    queryFn: () => endpoints.getApproval(org, id as string),
    enabled: !!id,
  });
