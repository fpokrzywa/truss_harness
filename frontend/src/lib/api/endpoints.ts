import { api as defaultClient, type ApiClient } from './client';
import { createBuildEndpoints } from './build-endpoints';
import { createOntologyEndpoints } from './ontology-endpoints';
import { createGovernanceEndpoints } from './governance-endpoints';
import { createIntegrateEndpoints } from './integrate-endpoints';
import { createOperateEndpoints } from './operate-endpoints';
import { createPipelineEndpoints } from './pipeline-endpoints';
import { createSecurityEndpoints } from './security-endpoints';
import type {
  AuthConfig,
  CreateOrgInput,
  CreateScopeInput,
  Dashboard,
  ListResponse,
  MyPermissions,
  Org,
  PlatformInstallation,
  PlatformStatus,
  Project,
  Readiness,
  Session,
  SetupRedeemResult,
  Workspace,
} from './types';

const enc = encodeURIComponent;

export const orgPath = (org: string) => `/api/v1/orgs/${enc(org)}`;
export const wsPath = (org: string, ws: string) => `${orgPath(org)}/workspaces/${enc(ws)}`;

export function createEndpoints(client: ApiClient = defaultClient) {
  return {
    authConfig: () =>
      client.request<AuthConfig>('/api/auth/config', { redirectOnUnauthorized: false }),
    session: () => client.getSession(),
    login: async (email: string, password: string): Promise<Session> => {
      const session = await client.request<Session>('/api/auth/local/login', {
        method: 'POST',
        body: { email, password },
        redirectOnUnauthorized: false,
        csrf: false,
      });
      client.setCsrfToken(session.csrf_token);
      return session;
    },
    logout: async (): Promise<void> => {
      await client.request<void>('/api/auth/logout', {
        method: 'POST',
        redirectOnUnauthorized: false,
      });
      client.setCsrfToken(undefined);
    },

    redeemSetup: async (token: string, password?: string): Promise<SetupRedeemResult> => {
      const result = await client.request<SetupRedeemResult>('/api/auth/setup/redeem', {
        method: 'POST',
        body: password === undefined ? { token } : { token, password },
        redirectOnUnauthorized: false,
        csrf: false, // no session yet
      });
      if ('csrf_token' in result) client.setCsrfToken(result.csrf_token);
      return result;
    },

    // /api/installation/branding is not ported (B-1101): the UI has no runtime branding.
    platformInstallation: () =>
      client.request<PlatformInstallation>('/api/v1/platform/installation'),

    platformStatus: () => client.request<PlatformStatus>('/api/v1/platform/status'),

    listOrgs: (all = false) =>
      client.request<ListResponse<Org>>('/api/v1/orgs', {
        query: { limit: 200, scope: all ? 'all' : undefined },
      }),
    createOrg: (input: CreateOrgInput) =>
      client.request<Org>('/api/v1/orgs', { method: 'POST', body: input }),
    dashboard: (org: string, workspace?: string, project?: string) =>
      client.request<Dashboard>(`${orgPath(org)}/dashboard`, { query: { workspace, project } }),

    listWorkspaces: (org: string, includeArchived = false) =>
      client.request<ListResponse<Workspace>>(`${orgPath(org)}/workspaces`, {
        query: { limit: 200, include_archived: includeArchived ? 'true' : undefined },
      }),
    createWorkspace: (org: string, input: CreateScopeInput) =>
      client.request<Workspace>(`${orgPath(org)}/workspaces`, { method: 'POST', body: input }),
    archiveWorkspace: (org: string, ws: Workspace) =>
      client.request<Workspace>(wsPath(org, ws.slug), {
        method: 'PATCH',
        body: { status: 'archived', row_version: ws.row_version },
      }),
    listProjects: (org: string, ws: string, includeArchived = false) =>
      client.request<ListResponse<Project>>(`${wsPath(org, ws)}/projects`, {
        query: { limit: 200, include_archived: includeArchived ? 'true' : undefined },
      }),
    createProject: (org: string, ws: string, input: CreateScopeInput) =>
      client.request<Project>(`${wsPath(org, ws)}/projects`, { method: 'POST', body: input }),
    archiveProject: (org: string, ws: string, proj: Project) =>
      client.request<Project>(`${wsPath(org, ws)}/projects/${enc(proj.slug)}`, {
        method: 'PATCH',
        body: { status: 'archived', row_version: proj.row_version },
      }),
    ...createSecurityEndpoints(client),
    ...createIntegrateEndpoints(client),
    ...createGovernanceEndpoints(client),
    ...createBuildEndpoints(client),
    ...createOntologyEndpoints(client),
    ...createOperateEndpoints(client),
    ...createPipelineEndpoints(client),
    myPermissions: (org: string, workspace?: string, project?: string) =>
      client.request<MyPermissions>(`${orgPath(org)}/me/permissions`, {
        query: { workspace, project },
      }),

    ready: () => client.getTolerant<Readiness>('/api/ready'),
  };
}

export type Endpoints = ReturnType<typeof createEndpoints>;
export const endpoints = createEndpoints();
