'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import type { EvaluationRunOut } from './api/evaluation-types';
import { endpoints } from './api/endpoints';
import { POLL_MS } from './operate-hooks';

export type ReleaseKind =
  'solutions' | 'versions' | 'releases' | 'evaluations' | 'datasets' | 'evaluation-runs';

const key = (org: string, kind: ReleaseKind, ...rest: unknown[]) =>
  ['releases', org, kind, ...rest] as const;

const active = (run: EvaluationRunOut) => run.status === 'queued' || run.status === 'running';

/**
 * Polls every second while an evaluation is queued or running; the function form returns false
 * once every run is done, and TanStack pauses while the tab is hidden.
 */
const pollWhile = (runs: EvaluationRunOut[] | undefined): number | false =>
  runs?.some(active) ? POLL_MS : false;

/** First page of the project's solutions, active and archived. */
export const useSolutions = (org: string, ws: string, proj: string) =>
  useQuery({
    queryKey: key(org, 'solutions', ws, proj),
    queryFn: () => endpoints.listSolutions(org, ws, proj),
    enabled: !!ws && !!proj,
  });

export const useSolution = (org: string, ws: string, proj: string, id: string) =>
  useQuery({
    queryKey: key(org, 'solutions', ws, proj, 'item', id),
    queryFn: () => endpoints.getSolution(org, ws, proj, id),
    enabled: !!ws && !!proj,
  });

export const useSolutionVersions = (org: string, ws: string, proj: string, id: string) =>
  useQuery({
    queryKey: key(org, 'versions', ws, proj, id),
    queryFn: () => endpoints.listSolutionVersions(org, ws, proj, id),
    enabled: !!ws && !!proj,
  });

export const useVersionReleases = (
  org: string,
  ws: string,
  proj: string,
  id: string,
  vid: string,
) =>
  useQuery({
    queryKey: key(org, 'releases', ws, proj, id, vid),
    queryFn: () => endpoints.listVersionReleases(org, ws, proj, id, vid),
    enabled: !!ws && !!proj,
  });

export const useRelease = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'releases', 'item', id),
    queryFn: () => endpoints.getRelease(org, id),
  });

/** The release's evaluation runs, polled while any of them is queued or running. */
export const useReleaseEvaluations = (org: string, releaseId: string) =>
  useQuery({
    queryKey: key(org, 'evaluations', releaseId),
    queryFn: () => endpoints.listReleaseEvaluations(org, releaseId),
    refetchInterval: (q) => pollWhile(q.state.data?.items),
    refetchIntervalInBackground: false,
  });

/** First page of the project's evaluation datasets (every version). */
export const useDatasets = (org: string, ws: string, proj: string) =>
  useQuery({
    queryKey: key(org, 'datasets', ws, proj),
    queryFn: () => endpoints.listDatasets(org, ws, proj),
    enabled: !!ws && !!proj,
  });

/** One evaluation run, polled while it is queued or running. */
export const useEvaluationRun = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'evaluation-runs', id),
    queryFn: () => endpoints.getEvaluationRun(org, id),
    refetchInterval: (q) => pollWhile(q.state.data ? [q.state.data] : undefined),
    refetchIntervalInBackground: false,
  });

/** Refetches every cached query of the given release resource kinds. */
export function useRefreshReleases(org: string) {
  const qc = useQueryClient();
  return useCallback(
    (...kinds: ReleaseKind[]) =>
      Promise.all(kinds.map((k) => qc.invalidateQueries({ queryKey: ['releases', org, k] }))),
    [qc, org],
  );
}
