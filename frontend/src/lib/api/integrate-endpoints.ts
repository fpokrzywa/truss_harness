import type { ApiClient } from './client';
import type {
  Adapter,
  Allowlist,
  ChatMessage,
  ConformanceRun,
  Connection,
  ConnectionInput,
  ConnectionInvokeResult,
  ConnectionStatus,
  ConnectionTestJob,
  ConnectionTestResult,
  DiscoveredModel,
  Discovery,
  DiscoveryJob,
  EgressPolicy,
  EgressPolicyInput,
  EgressRules,
  HealthCheckJob,
  HealthResult,
  InvokeModelResult,
  Model,
  ModelInput,
  ModelProvider,
  ModelRoute,
  ProviderInput,
  RouteInput,
  Vendor,
  VendorCapability,
  VendorInput,
  VendorStatus,
} from './integrate-types';
import { ApiError } from './errors';
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
type Query = Record<string, string | number | undefined>;

/**
 * Status poll of a queued connection test, provider health check or model discovery: the interval
 * of `POLL_MS` (operate-hooks imports these endpoints, so it is not imported here), capped at 60 s.
 */
const TEST_POLL_MS = 1000;
const TEST_TIMEOUT_MS = 60_000;
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

interface QueuedJob<R> {
  id: string;
  status: 'queued' | 'running' | 'done' | 'failed';
  result: R | null;
  error: string | null;
}

/** How a polled job is named in its errors: code prefix, title and the noun of the detail. */
interface JobLabel {
  code: string;
  title: string;
  noun: string;
}

/** Poll a queued job until the worker has recorded its result; a failed job or a timeout throws. */
async function awaitJob<R>(
  first: QueuedJob<R>,
  poll: (jobId: string) => Promise<QueuedJob<R>>,
  label: JobLabel,
): Promise<R> {
  let job = first;
  const deadline = Date.now() + TEST_TIMEOUT_MS;
  while (job.status === 'queued' || job.status === 'running') {
    if (Date.now() >= deadline) {
      throw new ApiError({
        status: 504,
        code: `${label.code}_timeout`,
        title: `${label.title} did not finish`,
        detail: `The ${label.noun} did not finish within 60 seconds. Try again later.`,
      });
    }
    await pause(TEST_POLL_MS);
    job = await poll(job.id);
  }
  if (job.status === 'done' && job.result) return job.result;
  throw new ApiError({
    status: 409,
    code: job.error ?? `${label.code}_failed`,
    title: `${label.title} did not run`,
    detail: `The ${label.noun} did not run (${job.error ?? 'unknown error'}).`,
  });
}

/** Accept either a list envelope or a bare array (the contract shows bare arrays for some GETs). */
function items<T>(res: ListResponse<T> | T[]): T[] {
  return Array.isArray(res) ? res : res.items;
}

/** Phase 3 routes (docs/api/PHASE_3_CONTRACT.md sections 1-4). */
export function createIntegrateEndpoints(client: ApiClient) {
  const list = <T>(path: string, query?: Query) =>
    client.request<ListResponse<T>>(path, { query: { limit: 200, ...query } });
  const send = <T>(method: 'POST' | 'PATCH' | 'PUT', path: string, body: unknown) =>
    client.request<T>(path, { method, body });

  return {
    // Adapters
    listAdapters: async () =>
      items(await client.request<ListResponse<Adapter> | Adapter[]>('/api/v1/adapters')),
    listConformanceRuns: async (key: string) =>
      items(
        await client.request<ListResponse<ConformanceRun> | ConformanceRun[]>(
          `/api/v1/adapters/${enc(key)}/conformance-runs`,
        ),
      ),

    // Vendors
    listVendors: (org: string, q: { category?: string; status?: string } = {}) =>
      list<Vendor>(`${org$(org)}/vendors`, q),
    getVendor: (org: string, id: string) =>
      client.request<Vendor>(`${org$(org)}/vendors/${enc(id)}`),
    createVendor: (org: string, input: VendorInput) =>
      send<Vendor>('POST', `${org$(org)}/vendors`, input),
    updateVendor: (
      org: string,
      id: string,
      input: Partial<VendorInput> & { row_version: number },
    ) => send<Vendor>('PATCH', `${org$(org)}/vendors/${enc(id)}`, input),
    setVendorStatus: (org: string, v: Vendor, status: VendorStatus) =>
      send<Vendor>('POST', `${org$(org)}/vendors/${enc(v.id)}/status`, {
        status,
        row_version: v.row_version,
      }),
    vendorCapabilities: async (org: string, id: string) =>
      items(
        await client.request<ListResponse<VendorCapability> | VendorCapability[]>(
          `${org$(org)}/vendors/${enc(id)}/capabilities`,
        ),
      ),
    listPlatformVendors: () => list<Vendor>('/api/v1/platform/vendors'),
    createPlatformVendor: (input: VendorInput) =>
      send<Vendor>('POST', '/api/v1/platform/vendors', input),
    updatePlatformVendor: (
      id: string,
      input: Partial<VendorInput> & { row_version: number; status?: VendorStatus },
    ) => send<Vendor>('PATCH', `/api/v1/platform/vendors/${enc(id)}`, input),

    // Providers
    listProviders: (org: string) => list<ModelProvider>(`${org$(org)}/model-providers`),
    getProvider: (org: string, id: string) =>
      client.request<ModelProvider>(`${org$(org)}/model-providers/${enc(id)}`),
    createProvider: (org: string, input: ProviderInput) =>
      send<ModelProvider>('POST', `${org$(org)}/model-providers`, input),
    /** Queue the health check (202), then poll its job until the worker has recorded it. */
    healthCheck: async (org: string, id: string): Promise<HealthResult> => {
      const path = `${org$(org)}/model-providers/${enc(id)}`;
      return awaitJob(
        await send<HealthCheckJob>('POST', `${path}/health-check`, {}),
        (jobId) => client.request<HealthCheckJob>(`${path}/health-check-jobs/${enc(jobId)}`),
        { code: 'health_check', title: 'Health check', noun: 'health check' },
      );
    },
    /** Queue the model discovery (202), then poll its job until the worker has recorded it. */
    discoverModels: async (org: string, id: string): Promise<DiscoveredModel[]> => {
      const path = `${org$(org)}/model-providers/${enc(id)}`;
      return items(
        await awaitJob(
          await send<DiscoveryJob>('POST', `${path}/discover-models`, {}),
          (jobId) => client.request<DiscoveryJob>(`${path}/discovery-jobs/${enc(jobId)}`),
          { code: 'discovery', title: 'Discovery', noun: 'model discovery' },
        ),
      );
    },

    // Models
    listModels: (org: string) => list<Model>(`${org$(org)}/models`),
    getModel: (org: string, id: string) => client.request<Model>(`${org$(org)}/models/${enc(id)}`),
    createModel: (org: string, input: ModelInput) =>
      send<Model>('POST', `${org$(org)}/models`, input),
    updateModel: (org: string, id: string, input: Partial<ModelInput> & { row_version: number }) =>
      send<Model>('PATCH', `${org$(org)}/models/${enc(id)}`, input),
    effectiveModels: (org: string, workspace?: string, project?: string) =>
      list<Model>(`${org$(org)}/models/effective`, { workspace, project }),
    getAllowlist: (org: string, scopeType: string, scopeId: string) =>
      client.request<Allowlist>(`${org$(org)}/model-allowlists`, {
        query: { scope_type: scopeType, scope_id: scopeId },
      }),
    putAllowlist: (org: string, scopeType: string, scopeId: string, body: Allowlist) =>
      client.request<Allowlist>(`${org$(org)}/model-allowlists`, {
        method: 'PUT',
        query: { scope_type: scopeType, scope_id: scopeId },
        body,
      }),
    listRoutes: (org: string) => list<ModelRoute>(`${org$(org)}/model-routes`),
    createRoute: (org: string, input: RouteInput) =>
      send<ModelRoute>('POST', `${org$(org)}/model-routes`, input),
    updateRoute: (org: string, id: string, input: Partial<RouteInput> & { row_version: number }) =>
      send<ModelRoute>('PATCH', `${org$(org)}/model-routes/${enc(id)}`, input),
    invokeModel: (org: string, id: string, messages: ChatMessage[], maxTokens?: number) =>
      send<InvokeModelResult>('POST', `${org$(org)}/models/${enc(id)}/invoke`, {
        messages,
        ...(maxTokens !== undefined ? { max_tokens: maxTokens } : {}),
      }),

    // Connections
    listConnections: (org: string, q: Query = {}) =>
      list<Connection>(`${org$(org)}/connections`, q),
    getConnection: (org: string, id: string) =>
      client.request<Connection>(`${org$(org)}/connections/${enc(id)}`),
    createConnection: (org: string, input: ConnectionInput) =>
      send<Connection>('POST', `${org$(org)}/connections`, input),
    setConnectionStatus: (org: string, c: Connection, status: ConnectionStatus) =>
      send<Connection>('POST', `${org$(org)}/connections/${enc(c.id)}/status`, {
        status,
        row_version: c.row_version,
      }),
    getConnectionTestJob: (org: string, id: string, jobId: string) =>
      client.request<ConnectionTestJob>(
        `${org$(org)}/connections/${enc(id)}/test-jobs/${enc(jobId)}`,
      ),
    /** Queue the test (202), then poll its job until the worker has recorded the result. */
    testConnection: async (org: string, id: string): Promise<ConnectionTestResult> =>
      awaitJob(
        await send<ConnectionTestJob>('POST', `${org$(org)}/connections/${enc(id)}/test`, {}),
        (jobId) =>
          client.request<ConnectionTestJob>(
            `${org$(org)}/connections/${enc(id)}/test-jobs/${enc(jobId)}`,
          ),
        { code: 'test', title: 'Test', noun: 'connection test' },
      ),
    listConnectionTests: (org: string, id: string) =>
      list<ConnectionTestResult>(`${org$(org)}/connections/${enc(id)}/tests`),
    discoverConnection: (org: string, id: string) =>
      send<Discovery>('POST', `${org$(org)}/connections/${enc(id)}/discover`, {}),
    latestDiscovery: (org: string, id: string) =>
      client.request<Discovery>(`${org$(org)}/connections/${enc(id)}/discoveries/latest`),
    invokeConnection: (org: string, id: string, body: Record<string, unknown>) =>
      send<ConnectionInvokeResult>('POST', `${org$(org)}/connections/${enc(id)}/invoke`, body),

    // Egress
    listEgressPolicies: (org: string) => list<EgressPolicy>(`${org$(org)}/egress-policies`),
    createEgressPolicy: (org: string, input: EgressPolicyInput) =>
      send<EgressPolicy>('POST', `${org$(org)}/egress-policies`, input),
    updateEgressPolicy: (
      org: string,
      id: string,
      input: Partial<EgressPolicyInput> & { row_version: number },
    ) => send<EgressPolicy>('PATCH', `${org$(org)}/egress-policies/${enc(id)}`, input),
    getEgressDefault: () => client.request<EgressRules>('/api/v1/platform/egress-default'),
    putEgressDefault: (rules: EgressRules) =>
      send<EgressRules>('PUT', '/api/v1/platform/egress-default', rules),
  };
}
