'use client';

import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo } from 'react';
import { endpoints } from './api/endpoints';
import { ApiError } from './api/errors';
import type { Artifact } from './api/build-types';
import { useDraft, useVersion } from './build-hooks';
import { normalizeDiscovery, type DiscoveredTable } from './discovery';
import { normalizeOntologySpec } from './ontology-spec';

const key = (org: string, id: string, ...rest: unknown[]) =>
  ['ontology', org, id, ...rest] as const;

export const useMappings = (org: string, id: string, vid: string, enabled = true) =>
  useQuery({
    queryKey: key(org, id, 'mappings', vid),
    queryFn: () => endpoints.listMappings(org, id, vid),
    enabled,
  });

export const usePolicies = (org: string, id: string) =>
  useQuery({ queryKey: key(org, id, 'policies'), queryFn: () => endpoints.listPolicies(org, id) });

export const useLineage = (org: string, id: string, runId: string) =>
  useInfiniteQuery({
    queryKey: key(org, id, 'lineage', runId),
    queryFn: ({ pageParam }) => endpoints.listLineage(org, id, runId, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const useObjectView = (
  org: string,
  id: string,
  objectType: string | undefined,
  pk: string | undefined,
  versionId?: string,
) =>
  useQuery({
    queryKey: key(org, id, 'object', objectType, pk, versionId),
    queryFn: () => endpoints.getObject(org, id, objectType as string, pk as string, versionId),
    enabled: !!objectType && !!pk,
    retry: false,
  });

/** Relations (with columns) from the connection's latest schema discovery; 404 = none yet (empty). */
export function useConnectionTables(org: string, connectionId: string) {
  const q = useQuery({
    queryKey: ['int', org, 'connections', 'discovery', connectionId, 'ontology'],
    queryFn: async (): Promise<DiscoveredTable[] | null> => {
      try {
        return normalizeDiscovery(await endpoints.latestDiscovery(org, connectionId)).tables;
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    enabled: !!connectionId,
    retry: false,
  });
  return { ...q, tables: q.data ?? [], noDiscovery: q.data === null };
}

/** Working spec for panels outside the editor: the saved draft, else the latest published version. */
export function useOntologySpec(org: string, artifact: Artifact) {
  const draft = useDraft(org, 'ontologies', artifact.id);
  const latestId = draft.data === null ? artifact.latest_published_version?.id : undefined;
  const latest = useVersion(org, 'ontologies', artifact.id, latestId);
  const source = draft.data?.spec ?? latest.data?.spec;
  const spec = useMemo(() => normalizeOntologySpec(source), [source]);
  const pending = draft.isPending || (!!latestId && latest.isPending);
  const error = draft.error ?? latest.error;
  return { spec, pending, error };
}

export function useRefreshOntology(org: string, id: string) {
  const qc = useQueryClient();
  return useCallback(
    () => qc.invalidateQueries({ queryKey: ['ontology', org, id] }),
    [qc, org, id],
  );
}
