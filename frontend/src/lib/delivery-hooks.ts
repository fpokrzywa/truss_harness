'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import type { PlanStatus } from './api/delivery-types';
import { endpoints } from './api/endpoints';

export type DeliveryKind = 'targets' | 'executors' | 'credentials' | 'plans' | 'deployments';

const key = (org: string, kind: DeliveryKind, ...rest: unknown[]) =>
  ['delivery', org, kind, ...rest] as const;

/** First page of the organization's targets, active and archived. */
export const useTargets = (org: string, enabled = true) =>
  useQuery({ queryKey: key(org, 'targets'), queryFn: () => endpoints.listTargets(org), enabled });

export const useExecutors = (org: string) =>
  useQuery({ queryKey: key(org, 'executors'), queryFn: () => endpoints.listExecutors(org) });

/** Credential metadata only; issued secrets never enter the query cache. */
export const useExecutorCredentials = (org: string, executorId: string) =>
  useQuery({
    queryKey: key(org, 'credentials', executorId),
    queryFn: () => endpoints.listExecutorCredentials(org, executorId),
  });

/** First page of the organization's plans, optionally one status only. */
export const usePlans = (org: string, q: { status?: PlanStatus } = {}) =>
  useQuery({ queryKey: key(org, 'plans', q), queryFn: () => endpoints.listPlans(org, q) });

export const usePlan = (org: string, id: string) =>
  useQuery({ queryKey: key(org, 'plans', 'item', id), queryFn: () => endpoints.getPlan(org, id) });

export const useDeployments = (org: string) =>
  useQuery({ queryKey: key(org, 'deployments'), queryFn: () => endpoints.listDeployments(org) });

/** The deployment with its step journal. */
export const useDeployment = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'deployments', 'item', id),
    queryFn: () => endpoints.getDeployment(org, id),
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
