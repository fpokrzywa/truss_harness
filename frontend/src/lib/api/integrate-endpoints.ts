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
  ConnectionTestResult,
  DiscoveredModel,
  Discovery,
  EgressPolicy,
  EgressPolicyInput,
  EgressRules,
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
import type { ListResponse } from './types';

const enc = encodeURIComponent;
const org$ = (org: string) => `/api/v1/orgs/${enc(org)}`;
type Query = Record<string, string | number | undefined>;

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
    healthCheck: (org: string, id: string) =>
      send<HealthResult>('POST', `${org$(org)}/model-providers/${enc(id)}/health-check`, {}),
    discoverModels: async (org: string, id: string) =>
      items(
        await client.request<ListResponse<DiscoveredModel> | DiscoveredModel[]>(
          `${org$(org)}/model-providers/${enc(id)}/discover-models`,
          { method: 'POST', body: {} },
        ),
      ),

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
    testConnection: (org: string, id: string) =>
      send<ConnectionTestResult>('POST', `${org$(org)}/connections/${enc(id)}/test`, {}),
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
