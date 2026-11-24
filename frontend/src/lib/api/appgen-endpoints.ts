import type { ApiClient } from './client';
import type {
  AcceptIn,
  AppRequestCreate,
  AppRequestOut,
  AppRequestSummary,
  DraftEdit,
  DraftOut,
  RequestStatus,
} from './appgen-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
const req$ = (org: string, ws: string, proj: string) =>
  `${org$(org)}/workspaces/${enc(ws)}/projects/${enc(proj)}/app-requests`;
const one$ = (org: string, id: string) => `${org$(org)}/app-requests/${enc(id)}`;

/** App requests and their drafts (`shifde.appgen.api`). */
export function createAppgenEndpoints(client: ApiClient) {
  const post = <T>(path: string, body: unknown) =>
    client.request<T>(path, { method: 'POST', body });

  return {
    createAppRequest: (org: string, ws: string, proj: string, input: AppRequestCreate) =>
      post<AppRequestOut>(req$(org, ws, proj), input),
    listAppRequests: (
      org: string,
      ws: string,
      proj: string,
      q: { status?: RequestStatus; limit?: number; cursor?: string } = {},
    ) => client.request<ListResponse<AppRequestSummary>>(req$(org, ws, proj), { query: q }),
    getAppRequest: (org: string, id: string) => client.request<AppRequestOut>(one$(org, id)),
    reviseAppDraft: (org: string, id: string, input: DraftEdit) =>
      post<DraftOut>(`${one$(org, id)}/drafts`, input),
    acceptAppRequest: (org: string, id: string, input: AcceptIn) =>
      post<AppRequestOut>(`${one$(org, id)}/accept`, input),
    discardAppRequest: (org: string, id: string) =>
      post<AppRequestOut>(`${one$(org, id)}/discard`, {}),
    retryAppRequest: (org: string, id: string) => post<AppRequestOut>(`${one$(org, id)}/retry`, {}),
  };
}
