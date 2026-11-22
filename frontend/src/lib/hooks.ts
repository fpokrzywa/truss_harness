'use client';

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { endpoints } from './api/endpoints';

export const keys = {
  session: ['session'] as const,
  authConfig: ['auth-config'] as const,
  orgs: (all: boolean) => ['orgs', all] as const,
  workspaces: (org: string, archived = false) => ['workspaces', org, archived] as const,
  projects: (org: string, ws: string, archived = false) => ['projects', org, ws, archived] as const,
  dashboard: (org: string, ws?: string, proj?: string) => ['dashboard', org, ws, proj] as const,
  permissions: (org: string, ws?: string, proj?: string) => ['permissions', org, ws, proj] as const,
  platformStatus: ['platform-status'] as const,
  ready: ['ready'] as const,
};

export const useSession = () =>
  useQuery({ queryKey: keys.session, queryFn: () => endpoints.session(), retry: false });

export const useAuthConfig = () =>
  useQuery({ queryKey: keys.authConfig, queryFn: () => endpoints.authConfig(), retry: false });

export const useOrgs = (all = false) =>
  useQuery({ queryKey: keys.orgs(all), queryFn: () => endpoints.listOrgs(all) });

export const useWorkspaces = (org: string | undefined, includeArchived = false) =>
  useQuery({
    queryKey: keys.workspaces(org ?? '', includeArchived),
    queryFn: () => endpoints.listWorkspaces(org as string, includeArchived),
    enabled: !!org,
    placeholderData: keepPreviousData,
  });

export const useProjects = (
  org: string | undefined,
  ws: string | undefined,
  includeArchived = false,
) =>
  useQuery({
    queryKey: keys.projects(org ?? '', ws ?? '', includeArchived),
    queryFn: () => endpoints.listProjects(org as string, ws as string, includeArchived),
    enabled: !!org && !!ws,
    placeholderData: keepPreviousData,
  });

export const useDashboard = (org: string, ws?: string, proj?: string) =>
  useQuery({
    queryKey: keys.dashboard(org, ws, proj),
    queryFn: () => endpoints.dashboard(org, ws, proj),
  });

const toSet = (d: { permissions: string[] }): ReadonlySet<string> => new Set(d.permissions);

/**
 * Effective permissions at a scope (FR-UI-004). Hiding is convenience only; the server decides.
 * `keepPrevious` keeps the last scope's set as placeholder data while a new scope loads.
 */
export const usePermissions = (
  org: string | undefined,
  ws?: string,
  proj?: string,
  keepPrevious = false,
) =>
  useQuery({
    queryKey: keys.permissions(org ?? '', ws, proj),
    queryFn: () => endpoints.myPermissions(org as string, ws, proj),
    enabled: !!org,
    placeholderData: keepPrevious ? keepPreviousData : undefined,
    select: toSet,
    retry: false,
    staleTime: 60_000,
  });

/** Tenant-scope permission check. `can` is false until permissions load (mutations stay hidden). */
export function useCan(org: string | undefined) {
  const q = usePermissions(org);
  const data = q.data;
  return { loaded: q.isSuccess, can: (key: string) => data?.has(key) ?? false };
}
