'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { endpoints } from './api/endpoints';
import type { ArtifactFilters, ArtifactKind } from './api/build-types';

const key = (org: string, ...rest: unknown[]) => ['build', org, ...rest] as const;

export const useArtifacts = (org: string, kind: ArtifactKind, filters: ArtifactFilters = {}) =>
  useQuery({
    queryKey: key(org, kind, 'list', filters),
    queryFn: () => endpoints.listArtifacts(org, kind, filters),
  });

export const useArtifact = (org: string, kind: ArtifactKind, id: string) =>
  useQuery({
    queryKey: key(org, kind, 'one', id),
    queryFn: () => endpoints.getArtifact(org, kind, id),
  });

export const useVersions = (org: string, kind: ArtifactKind, id: string, enabled = true) =>
  useQuery({
    queryKey: key(org, kind, 'versions', id),
    queryFn: () => endpoints.listVersions(org, kind, id),
    enabled,
  });

export const useVersion = (org: string, kind: ArtifactKind, id: string, vid: string | undefined) =>
  useQuery({
    queryKey: key(org, kind, 'version', id, vid),
    queryFn: () => endpoints.getVersion(org, kind, id, vid as string),
    enabled: !!vid,
  });

export const useDraft = (org: string, kind: ArtifactKind, id: string) =>
  useQuery({
    queryKey: key(org, kind, 'draft', id),
    queryFn: () => endpoints.getDraft(org, kind, id),
    // The editor owns the working copy; never refetch over unsaved edits.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

export const usePublishRequests = (org: string) =>
  useQuery({
    queryKey: key(org, 'publish-requests'),
    queryFn: () => endpoints.listPublishRequests(org),
  });

export const useToolExecutors = () =>
  useQuery({
    queryKey: ['tool-executors'],
    queryFn: () => endpoints.toolExecutors(),
    staleTime: 60_000,
  });

export const usePromptTestRuns = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'prompts', 'test-runs', id),
    queryFn: () => endpoints.listPromptTestRuns(org, id),
  });

export const useResolvedAgentVersion = (org: string, id: string, vid: string | undefined) =>
  useQuery({
    queryKey: key(org, 'agents', 'resolved', id, vid),
    queryFn: () => endpoints.resolvedAgentVersion(org, id, vid as string),
    enabled: !!vid,
  });

/** Invalidates every cached Build query for an org (cheap and always correct after a mutation). */
export function useRefreshBuild(org: string) {
  const qc = useQueryClient();
  return useCallback(() => qc.invalidateQueries({ queryKey: ['build', org] }), [qc, org]);
}
