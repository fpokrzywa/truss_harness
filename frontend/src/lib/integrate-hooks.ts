'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { endpoints } from './api/endpoints';

export type IntKind =
  'vendors' | 'models' | 'providers' | 'routes' | 'connections' | 'egress' | 'allowlist';

const key = (org: string, kind: IntKind, ...rest: unknown[]) =>
  ['int', org, kind, ...rest] as const;

export const useAdapters = () =>
  useQuery({ queryKey: ['adapters'], queryFn: () => endpoints.listAdapters(), staleTime: 60_000 });

export const useConformanceRuns = (adapterKey: string) =>
  useQuery({
    queryKey: ['adapters', adapterKey, 'runs'],
    queryFn: () => endpoints.listConformanceRuns(adapterKey),
  });

export const useVendors = (org: string, q: { category?: string; status?: string } = {}) =>
  useQuery({ queryKey: key(org, 'vendors', q), queryFn: () => endpoints.listVendors(org, q) });

export const useVendor = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'vendors', 'one', id),
    queryFn: () => endpoints.getVendor(org, id),
  });

export const useVendorCapabilities = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'vendors', 'caps', id),
    queryFn: () => endpoints.vendorCapabilities(org, id),
  });

export const usePlatformVendors = () =>
  useQuery({
    queryKey: ['int', '_platform', 'vendors'],
    queryFn: () => endpoints.listPlatformVendors(),
  });

export const useProviders = (org: string) =>
  useQuery({ queryKey: key(org, 'providers'), queryFn: () => endpoints.listProviders(org) });

export const useProvider = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'providers', 'one', id),
    queryFn: () => endpoints.getProvider(org, id),
  });

export const useModels = (org: string) =>
  useQuery({ queryKey: key(org, 'models'), queryFn: () => endpoints.listModels(org) });

export const useModel = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'models', 'one', id),
    queryFn: () => endpoints.getModel(org, id),
  });

export const useEffectiveModels = (org: string, workspace?: string, project?: string) =>
  useQuery({
    queryKey: key(org, 'models', 'effective', workspace, project),
    queryFn: () => endpoints.effectiveModels(org, workspace, project),
  });

export const useAllowlist = (org: string, scopeType: string, scopeId: string) =>
  useQuery({
    queryKey: key(org, 'allowlist', scopeType, scopeId),
    queryFn: () => endpoints.getAllowlist(org, scopeType, scopeId),
    enabled: scopeId !== '',
  });

export const useRoutes = (org: string) =>
  useQuery({ queryKey: key(org, 'routes'), queryFn: () => endpoints.listRoutes(org) });

export const useConnections = (org: string, q: Record<string, string | undefined> = {}) =>
  useQuery({
    queryKey: key(org, 'connections', q),
    queryFn: () => endpoints.listConnections(org, q),
  });

export const useConnection = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'connections', 'one', id),
    queryFn: () => endpoints.getConnection(org, id),
  });

export const useConnectionTests = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'connections', 'tests', id),
    queryFn: () => endpoints.listConnectionTests(org, id),
  });

export const useLatestDiscovery = (org: string, id: string) =>
  useQuery({
    queryKey: key(org, 'connections', 'discovery', id),
    queryFn: () => endpoints.latestDiscovery(org, id),
    retry: false,
  });

export const useEgressPolicies = (org: string, enabled = true) =>
  useQuery({
    queryKey: key(org, 'egress'),
    queryFn: () => endpoints.listEgressPolicies(org),
    enabled,
  });

/** Refetches every cached query of the given Integrate resource kinds. */
export function useRefreshInt(org: string) {
  const qc = useQueryClient();
  return useCallback(
    (...kinds: IntKind[]) =>
      Promise.all(kinds.map((k) => qc.invalidateQueries({ queryKey: ['int', org, k] }))),
    [qc, org],
  );
}
