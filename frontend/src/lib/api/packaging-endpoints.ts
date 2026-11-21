import type { ApiClient } from './client';
import type {
  ReleaseCreate,
  ReleaseOut,
  SolutionCreate,
  SolutionOut,
  SolutionStatus,
  SolutionVersionCreate,
  SolutionVersionOut,
} from './packaging-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
const sol$ = (org: string, ws: string, proj: string) =>
  `${org$(org)}/workspaces/${enc(ws)}/projects/${enc(proj)}/solutions`;
const ver$ = (org: string, ws: string, proj: string, id: string) =>
  `${sol$(org, ws, proj)}/${enc(id)}/versions`;

/** Solutions, solution versions and releases (`shifde.packaging.api`); one page per list. */
export function createPackagingEndpoints(client: ApiClient) {
  const post = <T>(path: string, body: unknown) =>
    client.request<T>(path, { method: 'POST', body });

  return {
    // Solutions
    listSolutions: (org: string, ws: string, proj: string, q: { status?: SolutionStatus } = {}) =>
      client.request<ListResponse<SolutionOut>>(sol$(org, ws, proj), { query: q }),
    createSolution: (org: string, ws: string, proj: string, input: SolutionCreate) =>
      post<SolutionOut>(sol$(org, ws, proj), input),
    getSolution: (org: string, ws: string, proj: string, id: string) =>
      client.request<SolutionOut>(`${sol$(org, ws, proj)}/${enc(id)}`),
    archiveSolution: (org: string, ws: string, proj: string, id: string) =>
      post<SolutionOut>(`${sol$(org, ws, proj)}/${enc(id)}/archive`, {}),

    // Solution versions
    listSolutionVersions: (org: string, ws: string, proj: string, id: string) =>
      client.request<ListResponse<SolutionVersionOut>>(ver$(org, ws, proj, id)),
    createSolutionVersion: (
      org: string,
      ws: string,
      proj: string,
      id: string,
      input: SolutionVersionCreate,
    ) => post<SolutionVersionOut>(ver$(org, ws, proj, id), input),
    getSolutionVersion: (org: string, ws: string, proj: string, id: string, vid: string) =>
      client.request<SolutionVersionOut>(`${ver$(org, ws, proj, id)}/${enc(vid)}`),

    // Releases
    sealRelease: (
      org: string,
      ws: string,
      proj: string,
      id: string,
      vid: string,
      input: ReleaseCreate,
    ) => post<ReleaseOut>(`${ver$(org, ws, proj, id)}/${enc(vid)}/releases`, input),
    listVersionReleases: (org: string, ws: string, proj: string, id: string, vid: string) =>
      client.request<ListResponse<ReleaseOut>>(`${ver$(org, ws, proj, id)}/${enc(vid)}/releases`),
    getRelease: (org: string, id: string) =>
      client.request<ReleaseOut>(`${org$(org)}/releases/${enc(id)}`),
    withdrawRelease: (org: string, id: string, note: string) =>
      post<ReleaseOut>(`${org$(org)}/releases/${enc(id)}/withdraw`, { note }),
  };
}
