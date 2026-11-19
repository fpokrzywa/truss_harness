'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { endpoints } from './api/endpoints';

export type DeliveryKind = 'targets' | 'executors' | 'credentials';

const key = (org: string, kind: DeliveryKind, ...rest: unknown[]) =>
  ['delivery', org, kind, ...rest] as const;

/** First page of the organization's targets, active and archived. */
export const useTargets = (org: string) =>
  useQuery({ queryKey: key(org, 'targets'), queryFn: () => endpoints.listTargets(org) });

export const useExecutors = (org: string) =>
  useQuery({ queryKey: key(org, 'executors'), queryFn: () => endpoints.listExecutors(org) });

/** Credential metadata only; issued secrets never enter the query cache. */
export const useExecutorCredentials = (org: string, executorId: string) =>
  useQuery({
    queryKey: key(org, 'credentials', executorId),
    queryFn: () => endpoints.listExecutorCredentials(org, executorId),
  });

/** Refetches every cached query of the given delivery resource kinds. */
export function useRefreshDelivery(org: string) {
  const qc = useQueryClient();
  return useCallback(
    (...kinds: DeliveryKind[]) =>
      Promise.all(kinds.map((k) => qc.invalidateQueries({ queryKey: ['delivery', org, k] }))),
    [qc, org],
  );
}
