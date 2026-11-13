import { api as defaultClient, type ApiClient } from './client';
import type {
  AuthConfig,
  CreateOrgInput,
  Dashboard,
  ListResponse,
  MyPermissions,
  Org,
  Project,
  Readiness,
  Session,
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

    listOrgs: (all = false) =>
      client.request<ListResponse<Org>>('/api/v1/orgs', {
        query: { limit: 200, scope: all ? 'all' : undefined },
      }),
    // createOrg and dashboard stay in the shell only because the shell's client tests use them as
    // the exemplar POST and query-string calls; the tenancy area owns them.
    createOrg: (input: CreateOrgInput) =>
      client.request<Org>('/api/v1/orgs', { method: 'POST', body: input }),
    dashboard: (org: string, workspace?: string, project?: string) =>
      client.request<Dashboard>(`${orgPath(org)}/dashboard`, { query: { workspace, project } }),

    listWorkspaces: (org: string, includeArchived = false) =>
      client.request<ListResponse<Workspace>>(`${orgPath(org)}/workspaces`, {
        query: { limit: 200, include_archived: includeArchived ? 'true' : undefined },
      }),
    listProjects: (org: string, ws: string, includeArchived = false) =>
      client.request<ListResponse<Project>>(`${wsPath(org, ws)}/projects`, {
        query: { limit: 200, include_archived: includeArchived ? 'true' : undefined },
      }),
    // Per-area endpoint groups (security, integrate, build, operate, pipelines, ontology,
    // governance) are spread in here as each area lands. The shell only owns session plumbing.
    myPermissions: (org: string, workspace?: string, project?: string) =>
      client.request<MyPermissions>(`${orgPath(org)}/me/permissions`, {
        query: { workspace, project },
      }),

    ready: () => client.getTolerant<Readiness>('/api/ready'),
  };
}

export type Endpoints = ReturnType<typeof createEndpoints>;
export const endpoints = createEndpoints();
