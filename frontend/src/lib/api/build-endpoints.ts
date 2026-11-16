import type { ApiClient } from './client';
import type { Artifact, ArtifactFilters, ArtifactKind } from './build-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;

/** Slice of SETH's build endpoints: only the artifact list governance needs (B-2210). */
export function createBuildEndpoints(client: ApiClient) {
  return {
    listArtifacts: (org: string, kind: ArtifactKind, f: ArtifactFilters = {}) =>
      client.request<ListResponse<Artifact>>(`/api/v1/orgs/${enc(org)}/${kind}`, {
        query: { limit: 200, ...f },
      }),
  };
}
