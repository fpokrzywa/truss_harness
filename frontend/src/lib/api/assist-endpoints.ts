import type { ApiClient } from './client';
import type {
  AssistCatalogOut,
  AssistInstallIn,
  AssistRequestIn,
  AssistRequestOut,
  AssistRequestStatus,
  AssistRequestSummary,
  AssistSettingsOut,
  AssistSettingsPut,
  ProposalOut,
  SpecialistOut,
} from './assist-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}/assist`;
const proj$ = (org: string, ws: string, proj: string) =>
  `/api/v1/orgs/${enc(org)}/workspaces/${enc(ws)}/projects/${enc(proj)}/assist`;

/** Ask SETH: settings, specialists, requests and proposals (`shifde.assist.api`). */
export function createAssistEndpoints(client: ApiClient) {
  const post = <T>(path: string, body?: unknown) =>
    client.request<T>(path, { method: 'POST', body });

  return {
    getAssistCatalog: (org: string) => client.request<AssistCatalogOut>(`${org$(org)}/catalog`),
    getAssistSettings: (org: string) => client.request<AssistSettingsOut>(`${org$(org)}/settings`),
    putAssistSettings: (org: string, input: AssistSettingsPut) =>
      client.request<AssistSettingsOut>(`${org$(org)}/settings`, { method: 'PUT', body: input }),
    enableAssist: (org: string) => post<AssistSettingsOut>(`${org$(org)}/enable`),
    disableAssist: (org: string) => post<AssistSettingsOut>(`${org$(org)}/disable`),

    installSpecialists: (org: string, ws: string, proj: string, input: AssistInstallIn) =>
      post<SpecialistOut[]>(`${proj$(org, ws, proj)}/install`, input),
    listSpecialists: (org: string, ws: string, proj: string) =>
      client.request<SpecialistOut[]>(`${proj$(org, ws, proj)}/specialists`),
    enableSpecialist: (org: string, ws: string, proj: string, key: string) =>
      post<SpecialistOut>(`${proj$(org, ws, proj)}/specialists/${enc(key)}/enable`),
    disableSpecialist: (org: string, ws: string, proj: string, key: string) =>
      post<SpecialistOut>(`${proj$(org, ws, proj)}/specialists/${enc(key)}/disable`),

    /** 201, or 202 when queued for model routing; a refused request is a 201 too. */
    createAssistRequest: (org: string, ws: string, proj: string, input: AssistRequestIn) =>
      post<AssistRequestOut>(`${proj$(org, ws, proj)}/requests`, input),
    listAssistRequests: (
      org: string,
      ws: string,
      proj: string,
      q: { status?: AssistRequestStatus; limit?: number; cursor?: string } = {},
    ) =>
      client.request<ListResponse<AssistRequestSummary>>(`${proj$(org, ws, proj)}/requests`, {
        query: q,
      }),
    getAssistRequest: (org: string, id: string) =>
      client.request<AssistRequestOut>(`${org$(org)}/requests/${enc(id)}`),
    acceptProposal: (org: string, id: string) =>
      post<ProposalOut>(`${org$(org)}/proposals/${enc(id)}/accept`, {}),
    dismissProposal: (org: string, id: string) =>
      post<ProposalOut>(`${org$(org)}/proposals/${enc(id)}/dismiss`, {}),
  };
}
