'use client';

import { keepPreviousData, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { endpoints } from './api/endpoints';
import type { ApiKeyFilters, BindingFilters } from './api/security-types';
import { useWorkspaces } from './hooks';

export type SecKind =
  'users' | 'groups' | 'roles' | 'bindings' | 'service-accounts' | 'api-keys' | 'idps' | 'secrets';

const key = (org: string, kind: SecKind, ...rest: unknown[]) =>
  ['sec', org, kind, ...rest] as const;

export const useUsers = (org: string) =>
  useQuery({ queryKey: key(org, 'users'), queryFn: () => endpoints.listUsers(org) });

export const useGroups = (org: string) =>
  useQuery({ queryKey: key(org, 'groups'), queryFn: () => endpoints.listGroups(org) });

export const useGroup = (org: string, id: string) =>
  useQuery({ queryKey: key(org, 'groups', id), queryFn: () => endpoints.getGroup(org, id) });

export const useRoles = (org: string) =>
  useQuery({ queryKey: key(org, 'roles'), queryFn: () => endpoints.listRoles(org) });

export const usePermissionCatalog = () =>
  useQuery({
    queryKey: ['permission-catalog'],
    queryFn: () => endpoints.permissionCatalog(),
    staleTime: 5 * 60_000,
  });

export const useBindings = (org: string, filters: BindingFilters) =>
  useQuery({
    queryKey: key(org, 'bindings', filters),
    queryFn: () => endpoints.listBindings(org, filters),
  });

export const useServiceAccounts = (org: string) =>
  useQuery({
    queryKey: key(org, 'service-accounts'),
    queryFn: () => endpoints.listServiceAccounts(org),
  });

export const useApiKeys = (org: string, filters: ApiKeyFilters = {}) =>
  useQuery({
    queryKey: key(org, 'api-keys', filters),
    queryFn: () => endpoints.listApiKeys(org, filters),
    placeholderData: keepPreviousData,
  });

export const useIdps = (base: string) =>
  useQuery({ queryKey: ['idps', base], queryFn: () => endpoints.listIdps(base) });

export const usePlatformGroups = () =>
  useQuery({ queryKey: ['platform-groups'], queryFn: () => endpoints.listPlatformGroups() });

export const useSecrets = (org: string) =>
  useQuery({ queryKey: key(org, 'secrets'), queryFn: () => endpoints.listSecrets(org) });

/** Returns a function that refetches every cached query of one security resource kind. */
export function useRefresh(org: string) {
  const qc = useQueryClient();
  return useCallback(
    (...kinds: SecKind[]) =>
      Promise.all(kinds.map((k) => qc.invalidateQueries({ queryKey: ['sec', org, k] }))),
    [qc, org],
  );
}

export interface ScopeOption {
  id: string;
  label: string;
  type: 'workspace' | 'project';
  workspaceId: string;
}

/** Workspaces and their projects (including archived) as flat labelled options, for scope pickers. */
export function useScopeOptions(org: string) {
  const workspaces = useWorkspaces(org, true);
  const wsItems = workspaces.data?.items ?? [];
  const projectQueries = useQueries({
    queries: wsItems.map((ws) => ({
      queryKey: ['projects', org, ws.slug, true],
      queryFn: () => endpoints.listProjects(org, ws.slug, true),
    })),
  });
  const options: ScopeOption[] = [];
  wsItems.forEach((ws, i) => {
    options.push({ id: ws.id, label: ws.name, type: 'workspace', workspaceId: ws.id });
    for (const p of projectQueries[i]?.data?.items ?? []) {
      options.push({
        id: p.id,
        label: `${ws.name} / ${p.name}`,
        type: 'project',
        workspaceId: ws.id,
      });
    }
  });
  return { options, isLoading: workspaces.isPending };
}
