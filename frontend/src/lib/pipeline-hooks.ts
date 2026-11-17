'use client';

import { keepPreviousData, useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { endpoints } from './api/endpoints';
import type { Run } from './api/operate-types';
import type { NodeRunStatus } from './api/pipeline-types';
import { useRun } from './operate-hooks';
import { nodeDisplayStatus } from './pipeline-spec';
import { isTerminal } from './run-utils';

const POLL_MS = 1000;
const key = (org: string, ...rest: unknown[]) => ['pipelines', org, ...rest] as const;

/** The node-type registry; the builder palette is derived from it (enabled types only). */
export const useNodeTypes = () =>
  useQuery({
    queryKey: ['pipelines', 'node-types'],
    queryFn: () => endpoints.pipelineNodeTypes(),
    staleTime: 60_000,
  });

/**
 * Per-node statuses poll every second while the run is non-terminal. The run status is part of the key
 * so one final fetch happens when it turns terminal; TanStack pauses interval refetches in hidden tabs.
 */
export const useRunNodes = (org: string, runId: string | undefined, run: Run | undefined) =>
  useQuery({
    queryKey: key(org, 'run', runId, 'nodes', run?.status),
    queryFn: () => endpoints.runNodes(org, runId as string),
    enabled: !!runId && !!run,
    refetchInterval: run && isTerminal(run.status) ? false : POLL_MS,
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });

export const usePipelineRuns = (org: string, pipelineId: string) =>
  useInfiniteQuery({
    queryKey: key(org, pipelineId, 'runs'),
    queryFn: ({ pageParam }) => endpoints.listPipelineRuns(org, pipelineId, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const useRunArtifact = (org: string, runId: string, artifactId: string | undefined) =>
  useQuery({
    queryKey: key(org, 'run', runId, 'artifact', artifactId),
    queryFn: () => endpoints.runArtifact(org, runId, artifactId as string),
    enabled: !!artifactId,
    retry: false,
  });

export const usePipelineSchedules = (org: string, pipelineId: string) =>
  useQuery({
    queryKey: key(org, pipelineId, 'schedules'),
    queryFn: () => endpoints.listPipelineSchedules(org, pipelineId),
    retry: false,
  });

/** Run + per-node statuses for the canvas overlay (all polling stops when the run is terminal). */
export function useRunOverlay(org: string, runId: string | undefined) {
  const run = useRun(org, runId);
  const nodes = useRunNodes(org, runId, run.data);
  const statuses = useMemo(
    () =>
      runId && nodes.data
        ? new Map<string, NodeRunStatus>(nodes.data.map((n) => [n.node_id, nodeDisplayStatus(n)]))
        : undefined,
    [runId, nodes.data],
  );
  return { run: run.data, runError: run.error, nodes: nodes.data, statuses };
}
