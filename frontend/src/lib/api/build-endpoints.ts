import type { ApiClient } from './client';
import { ApiError } from './errors';
import type {
  AgentResolved,
  AgentTestResult,
  Artifact,
  ArtifactFilters,
  ArtifactKind,
  ArtifactStatus,
  ArtifactVersion,
  ArtifactVisibility,
  CreateArtifactInput,
  DiffChange,
  ExecutorCatalog,
  PromptTestRun,
  PublishRequest,
  PublishResult,
  RenderResult,
  ToolTestResult,
  ValidationResult,
} from './build-types';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
const art$ = (org: string, kind: ArtifactKind, id: string) => `${org$(org)}/${kind}/${enc(id)}`;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Phase 4 routes (docs/api/PHASE_4_CONTRACT.md). */
export function createBuildEndpoints(client: ApiClient) {
  const post = <T>(path: string, body: unknown) =>
    client.request<T>(path, { method: 'POST', body });

  return {
    listArtifacts: (org: string, kind: ArtifactKind, f: ArtifactFilters = {}) =>
      client.request<ListResponse<Artifact>>(`${org$(org)}/${kind}`, {
        query: { limit: 200, ...f },
      }),
    createArtifact: (org: string, kind: ArtifactKind, input: CreateArtifactInput) =>
      post<Artifact>(`${org$(org)}/${kind}`, input),
    getArtifact: (org: string, kind: ArtifactKind, id: string) =>
      client.request<Artifact>(art$(org, kind, id)),
    updateArtifact: (
      org: string,
      kind: ArtifactKind,
      id: string,
      body: {
        name?: string;
        description?: string;
        visibility?: ArtifactVisibility;
        status?: ArtifactStatus;
        row_version: number;
      },
    ) => client.request<Artifact>(art$(org, kind, id), { method: 'PATCH', body }),

    listVersions: async (org: string, kind: ArtifactKind, id: string) => {
      const res = await client.request<ListResponse<ArtifactVersion> | ArtifactVersion[]>(
        `${art$(org, kind, id)}/versions`,
      );
      return Array.isArray(res) ? res : res.items;
    },
    getVersion: (org: string, kind: ArtifactKind, id: string, vid: string) =>
      client.request<ArtifactVersion>(`${art$(org, kind, id)}/versions/${enc(vid)}`),
    setVersionState: (
      org: string,
      kind: ArtifactKind,
      id: string,
      vid: string,
      state: 'deprecated' | 'retired',
    ) => post<ArtifactVersion>(`${art$(org, kind, id)}/versions/${enc(vid)}/state`, { state }),

    /** 404 means "no draft yet" and resolves to null. */
    getDraft: async (org: string, kind: ArtifactKind, id: string) => {
      try {
        return await client.request<ArtifactVersion>(`${art$(org, kind, id)}/draft`);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    saveDraft: (
      org: string,
      kind: ArtifactKind,
      id: string,
      spec: Record<string, unknown>,
      rowVersion: number,
    ) =>
      client.request<ArtifactVersion>(`${art$(org, kind, id)}/draft`, {
        method: 'PUT',
        body: { spec, row_version: rowVersion },
      }),
    validateDraft: (org: string, kind: ArtifactKind, id: string) =>
      post<ValidationResult>(`${art$(org, kind, id)}/draft/validate`, {}),
    publish: async (
      org: string,
      kind: ArtifactKind,
      id: string,
      changeNote: string,
    ): Promise<PublishResult> => {
      const res = await post<unknown>(`${art$(org, kind, id)}/publish`, {
        change_note: changeNote,
      });
      if (isRecord(res) && isRecord(res.publish_request)) {
        return { kind: 'pending', request: res.publish_request as unknown as PublishRequest };
      }
      return { kind: 'published', version: res as ArtifactVersion };
    },
    diff: async (org: string, kind: ArtifactKind, id: string, from: string, to: string) => {
      const res = await client.request<{ changes: DiffChange[] }>(`${art$(org, kind, id)}/diff`, {
        query: { from, to },
      });
      return res.changes;
    },
    rollback: (org: string, kind: ArtifactKind, id: string, versionId: string) =>
      post<ArtifactVersion>(`${art$(org, kind, id)}/rollback`, { version_id: versionId }),

    listPublishRequests: (org: string, status?: string) =>
      client.request<ListResponse<PublishRequest>>(`${org$(org)}/publish-requests`, {
        query: { limit: 200, status },
      }),
    decidePublishRequest: (
      org: string,
      id: string,
      decision: 'approve' | 'reject',
      comment?: string,
    ) =>
      post<PublishRequest>(`${org$(org)}/publish-requests/${enc(id)}/decide`, {
        decision,
        ...(comment ? { comment } : {}),
      }),

    // Prompts
    renderPrompt: (
      org: string,
      id: string,
      versionId: string,
      variables: Record<string, unknown>,
    ) =>
      post<RenderResult>(`${art$(org, 'prompts', id)}/render`, {
        version_id: versionId,
        variables,
      }),
    testPrompt: (
      org: string,
      id: string,
      versionId: string,
      variables: Record<string, unknown>,
      modelId?: string,
    ) =>
      post<PromptTestRun>(`${art$(org, 'prompts', id)}/test`, {
        version_id: versionId,
        variables,
        ...(modelId ? { model_id: modelId } : {}),
      }),
    listPromptTestRuns: async (org: string, id: string) => {
      const res = await client.request<ListResponse<PromptTestRun> | PromptTestRun[]>(
        `${art$(org, 'prompts', id)}/test-runs`,
      );
      return Array.isArray(res) ? res : res.items;
    },

    // Tools
    toolExecutors: () => client.request<ExecutorCatalog>('/api/v1/tool-executors'),
    testTool: (
      org: string,
      id: string,
      versionId: string,
      args: Record<string, unknown>,
      acknowledge: boolean,
    ) =>
      post<ToolTestResult>(`${art$(org, 'tools', id)}/test`, {
        version_id: versionId,
        args,
        ...(acknowledge ? { acknowledge_side_effects: true } : {}),
      }),

    // Agents
    testAgent: (
      org: string,
      id: string,
      versionId: string,
      variables: Record<string, unknown>,
      input: string,
    ) =>
      post<AgentTestResult>(`${art$(org, 'agents', id)}/test`, {
        version_id: versionId,
        variables,
        input,
      }),
    setAgentStatus: (org: string, id: string, status: 'active' | 'disabled') =>
      post<Artifact>(`${art$(org, 'agents', id)}/status`, { status }),
    resolvedAgentVersion: (org: string, id: string, vid: string) =>
      client.request<AgentResolved>(`${art$(org, 'agents', id)}/versions/${enc(vid)}/resolved`),
  };
}
