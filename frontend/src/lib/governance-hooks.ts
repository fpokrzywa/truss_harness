'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { endpoints } from './api/endpoints';
import type {
  DecisionFilters,
  EventFilters,
  PerfGroupBy,
  UsageFilters,
} from './api/governance-types';

const key = (org: string, ...rest: unknown[]) => ['gov', org, ...rest] as const;

export const useUsageSummary = (org: string, filters: UsageFilters) =>
  useQuery({
    queryKey: key(org, 'usage', 'summary', filters),
    queryFn: () => endpoints.usageSummary(org, filters),
  });

export const useUsageEvents = (org: string, filters: Omit<EventFilters, 'cursor'>) =>
  useInfiniteQuery({
    queryKey: key(org, 'usage', 'events', filters),
    queryFn: ({ pageParam }) =>
      endpoints.listUsageEvents(org, { ...filters, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const useRunUsage = (org: string, runId: string) =>
  useQuery({
    queryKey: key(org, 'usage', 'run', runId),
    queryFn: () => endpoints.runUsage(org, runId),
  });

/** `base` is the tenant route prefix or `/api/v1/platform`. */
export const useRateCards = (org: string, platform: boolean) =>
  useQuery({
    queryKey: key(org, 'rate-cards', platform ? 'platform' : 'tenant'),
    queryFn: () => (platform ? endpoints.listPlatformRateCards() : endpoints.listRateCards(org)),
  });

export const useRateCardVersions = (org: string, base: string, id: string) =>
  useQuery({
    queryKey: key(org, 'rate-cards', 'versions', base, id),
    queryFn: () => endpoints.listRateCardVersions(base, id),
    enabled: !!id,
  });

export const useBudgets = (org: string) =>
  useQuery({ queryKey: key(org, 'budgets'), queryFn: () => endpoints.listBudgets(org) });

export const useBudgetStatus = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'budgets', 'status', id),
    queryFn: () => endpoints.budgetStatus(org, id),
    retry: false,
  });

export const useQuotas = (org: string) =>
  useQuery({ queryKey: key(org, 'quotas'), queryFn: () => endpoints.listQuotas(org) });

export const useGovOverview = (org: string) =>
  useQuery({
    queryKey: key(org, 'overview'),
    queryFn: () => endpoints.governanceOverview(org),
  });

export const useGovPolicies = (org: string) =>
  useQuery({ queryKey: key(org, 'policies'), queryFn: () => endpoints.listGovPolicies(org) });

export const useRetention = (org: string) =>
  useQuery({ queryKey: key(org, 'retention'), queryFn: () => endpoints.getRetention(org) });

export const useDecisions = (org: string, filters: Omit<DecisionFilters, 'cursor'>) =>
  useInfiniteQuery({
    queryKey: key(org, 'decisions', filters),
    queryFn: ({ pageParam }) =>
      endpoints.listDecisions(org, { ...filters, ...(pageParam ? { cursor: pageParam } : {}) }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const usePerformance = (
  org: string,
  filters: { group_by: PerfGroupBy; from?: string; to?: string },
) =>
  useQuery({
    queryKey: key(org, 'performance', filters),
    queryFn: () => endpoints.performance(org, filters),
  });

export const govKey = key;
