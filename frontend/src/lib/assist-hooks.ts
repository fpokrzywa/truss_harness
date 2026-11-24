'use client';

import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';
import type {
  AssistRequestOut,
  AssistRequestStatus,
  AssistSettingsOut,
  ProposalOut,
} from './api/assist-types';
import { endpoints } from './api/endpoints';
import { useProjects, useWorkspaces } from './hooks';
import { isTerminal } from './run-utils';

export const ASSIST_POLL_MS = 3000;
/** Polling gives up this long after the screen started it; a refresh checks again. */
export const ASSIST_POLL_LIMIT_MS = 10 * 60 * 1000;

const ROUTING: ReadonlySet<string> = new Set<AssistRequestStatus>([
  'queued',
  'classifying',
  'handing_over',
]);

type Progress = Pick<AssistRequestOut, 'status' | 'run'>;

/** True while the request is being routed or its run has not ended. */
export const isAssistWorking = (r: Progress | undefined): boolean =>
  r !== undefined && (ROUTING.has(r.status) || (r.run !== null && !isTerminal(r.run.status)));

/** The `refetchInterval` of one request: every 3 s while it is working, until the time limit. */
export const assistPollInterval = (r: Progress | undefined, timedOut: boolean): number | false =>
  !timedOut && isAssistWorking(r) ? ASSIST_POLL_MS : false;

const key = (org: string, ...rest: unknown[]) => ['assist', org, ...rest] as const;
const requestKey = (org: string, id: string) => key(org, 'request', id);
const settingsKey = (org: string) => key(org, 'settings');

export const useAssistCatalog = (org: string) =>
  useQuery({ queryKey: key(org, 'catalog'), queryFn: () => endpoints.getAssistCatalog(org) });

export const useAssistSettings = (org: string) =>
  useQuery({ queryKey: settingsKey(org), queryFn: () => endpoints.getAssistSettings(org) });

/** The specialists installed in one project. */
export const useSpecialists = (org: string, ws: string, proj: string) =>
  useQuery({
    queryKey: key(org, 'specialists', ws, proj),
    queryFn: () => endpoints.listSpecialists(org, ws, proj),
  });

/** The project's requests, newest first, paged through `next_cursor`. */
export const useAssistRequests = (
  org: string,
  ws: string,
  proj: string,
  status: AssistRequestStatus | '',
) =>
  useInfiniteQuery({
    queryKey: key(org, 'requests', ws, proj, status),
    queryFn: ({ pageParam }) =>
      endpoints.listAssistRequests(org, ws, proj, {
        ...(status ? { status } : {}),
        ...(pageParam ? { cursor: pageParam } : {}),
      }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

/**
 * One request, polled while it is working. Polling stops on any other state, and
 * `ASSIST_POLL_LIMIT_MS` after it started (`timedOut`). TanStack pauses it while the tab is hidden.
 */
export function useAssistRequest(org: string, id: string) {
  const [timedOut, setTimedOut] = useState(false);
  const query = useQuery({
    queryKey: requestKey(org, id),
    queryFn: () => endpoints.getAssistRequest(org, id),
    refetchInterval: (q) => assistPollInterval(q.state.data, timedOut),
    refetchIntervalInBackground: false,
  });
  const working = isAssistWorking(query.data);
  const [wasWorking, setWasWorking] = useState(working);
  if (working !== wasWorking) {
    setWasWorking(working);
    if (working) setTimedOut(false);
  }
  useEffect(() => {
    if (!working) return;
    const timer = setTimeout(() => setTimedOut(true), ASSIST_POLL_LIMIT_MS);
    return () => clearTimeout(timer);
  }, [working]);
  return { query, polling: working && !timedOut, timedOut: working && timedOut };
}

/** Stores a decision's answer in the cached request; a poll that started before it is dropped. */
export function useStoreProposal(org: string, requestId: string) {
  const qc = useQueryClient();
  return useCallback(
    async (proposal: ProposalOut) => {
      const queryKey = requestKey(org, requestId);
      await qc.cancelQueries({ queryKey });
      qc.setQueryData<AssistRequestOut>(
        queryKey,
        (r) =>
          r && {
            ...r,
            proposals: r.proposals.map((p) => (p.id === proposal.id ? proposal : p)),
          },
      );
    },
    [qc, org, requestId],
  );
}

/** Stores the settings a write answered with. */
export function useStoreAssistSettings(org: string) {
  const qc = useQueryClient();
  return useCallback(
    (settings: AssistSettingsOut) => qc.setQueryData(settingsKey(org), settings),
    [qc, org],
  );
}

/** The slugs of a request's project, archived ones included; null until both resolve. */
export function useProjectSlugs(org: string, workspaceId: string, projectId: string) {
  const workspaces = useWorkspaces(org, true);
  const ws = workspaces.data?.items.find((w) => w.id === workspaceId)?.slug;
  const projects = useProjects(org, ws, true);
  const proj = projects.data?.items.find((p) => p.id === projectId)?.slug;
  return ws !== undefined && proj !== undefined ? { ws, proj } : null;
}

/** Refetches every cached Ask SETH query of the organization. */
export function useRefreshAssist(org: string) {
  const qc = useQueryClient();
  return useCallback(() => qc.invalidateQueries({ queryKey: ['assist', org] }), [qc, org]);
}
