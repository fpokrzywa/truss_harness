/** Phase 3 shapes (docs/api/PHASE_3_CONTRACT.md). Fields marked optional are tolerated, not guaranteed. */

export type Maturity = 'planned' | 'experimental' | 'validated';

export interface Evidence {
  run_id: string;
  finished_at: string;
  suite_version: string;
}

export interface AdapterCapability {
  capability: string;
  maturity: Maturity;
  evidence?: Evidence | null;
}

export interface JsonSchemaProperty {
  type?: 'string' | 'number' | 'integer' | 'boolean' | 'array' | 'object';
  title?: string;
  description?: string;
  enum?: (string | number)[];
  default?: unknown;
  format?: string;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  items?: { type?: string; enum?: (string | number)[] };
}

export interface JsonSchema {
  type?: 'object';
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
}

export interface Adapter {
  key: string;
  kind: string;
  version: string;
  capabilities: AdapterCapability[];
  config_schema: JsonSchema;
  secret_fields: string[];
  auth_methods: string[];
}

export interface ConformanceRun {
  id: string;
  adapter_key?: string;
  adapter_version?: string;
  suite_version: string;
  started_at?: string;
  finished_at: string;
  outcome: 'pass' | 'fail';
  checks?: unknown;
  environment?: unknown;
}

export type VendorCategory = 'data_platform' | 'model_provider' | 'saas_api' | 'mcp' | 'other';
export type VendorStatus = 'active' | 'disabled' | 'retired';

export interface Vendor {
  id: string;
  key: string;
  name: string;
  category: VendorCategory;
  description?: string | null;
  version?: string | null;
  status: VendorStatus;
  /** Global catalog entries are read-only to tenants. Either flag or scope marks them. */
  is_global?: boolean;
  scope?: string | null;
  tenant_id?: string | null;
  configuration_requirements?: string[];
  adapter_keys?: string[];
  row_version: number;
}

export interface VendorCapability {
  capability: string;
  adapter_key: string | null;
  maturity: Maturity;
  evidence?: Evidence | null;
}

export interface VendorInput {
  key: string;
  name: string;
  category: VendorCategory;
  description?: string;
  version?: string;
  configuration_requirements?: string[];
  adapter_keys?: string[];
}

export type ModelCapability = 'chat' | 'tools' | 'vision' | 'json_mode' | 'embeddings';
export type ModelLifecycle = 'preview' | 'active' | 'deprecated' | 'retired';
export type HealthStatus = 'healthy' | 'degraded' | 'unavailable';

export interface ModelProvider {
  id: string;
  name: string;
  adapter_key: string;
  connection_id: string;
  settings?: Record<string, unknown> | null;
  health_status?: HealthStatus | null;
  health_checked_at?: string | null;
  row_version: number;
}

export interface ProviderInput {
  name: string;
  adapter_key: string;
  connection_id: string;
  settings?: Record<string, unknown>;
}

export interface HealthResult {
  status: HealthStatus;
  latency_ms: number;
  checked_at: string;
  detail?: string | null;
}

export interface DiscoveredModel {
  model_key: string;
  display_name?: string;
  context_window?: number;
  max_output_tokens?: number;
  capabilities?: ModelCapability[];
}

export interface RateLimits {
  rpm?: number;
  tpm?: number;
  max_concurrency?: number;
}

export interface Model {
  id: string;
  provider_id: string;
  model_key: string;
  display_name: string;
  family?: string | null;
  version_label?: string | null;
  capabilities: ModelCapability[];
  context_window: number;
  max_output_tokens: number;
  lifecycle: ModelLifecycle;
  deprecation_date?: string | null;
  rate_limits?: RateLimits;
  row_version: number;
}

export interface ModelInput {
  provider_id: string;
  model_key: string;
  display_name: string;
  family?: string;
  version_label?: string;
  capabilities: ModelCapability[];
  context_window: number;
  max_output_tokens: number;
  lifecycle: ModelLifecycle;
  deprecation_date?: string;
  rate_limits?: RateLimits;
}

export interface Allowlist {
  enabled: boolean;
  model_ids: string[];
}

export interface RouteScope {
  type: 'tenant' | 'workspace' | 'project';
  id: string;
}

export interface ModelRoute {
  id: string;
  name: string;
  scope: RouteScope;
  primary_model_id: string;
  fallback_model_ids: string[];
  conditions?: Record<string, unknown>;
  row_version: number;
}

export interface RouteInput {
  name: string;
  scope: RouteScope;
  primary_model_id: string;
  fallback_model_ids: string[];
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface InvokeModelResult {
  output: string;
  usage: { input_tokens: number; output_tokens: number };
  latency_ms: number;
  provider_request_id?: string | null;
}

export type ConnectionType = 'rest' | 'mcp' | 'database' | 'data_platform' | 'model_api';
export type ConnectionStatus = 'draft' | 'active' | 'disabled' | 'retired';
export type AccessMode = 'read' | 'read_write';

export interface Connection {
  id: string;
  name: string;
  connection_type: ConnectionType;
  adapter_key: string;
  vendor_id?: string | null;
  scope?: { workspace?: string | null; project?: string | null } | null;
  config: Record<string, unknown>;
  auth_method: string;
  secret_id?: string | null;
  egress_policy_id?: string | null;
  access_mode: AccessMode;
  status: ConnectionStatus;
  health?: string | null;
  last_tested_at?: string | null;
  row_version: number;
}

export interface ConnectionInput {
  name: string;
  scope: { workspace?: string; project?: string };
  connection_type: ConnectionType;
  adapter_key: string;
  vendor_id?: string;
  config: Record<string, unknown>;
  auth_method: string;
  secret_id?: string;
  egress_policy_id?: string;
  access_mode: AccessMode;
}

export type CheckName = 'config' | 'egress' | 'dns' | 'tls' | 'auth' | 'capability';

export interface TestCheck {
  name: CheckName | string;
  status: 'pass' | 'fail' | 'skipped' | string;
  detail?: string | null;
}

export interface ConnectionTestResult {
  id: string;
  outcome: 'pass' | 'fail';
  checks: TestCheck[];
  error_class?: string | null;
  duration_ms: number;
  created_at?: string;
}

/** Stored discovery. The contract gives no field names beyond the three kinds, so every one is tolerated. */
export interface Discovery {
  id?: string;
  kind?: 'openapi' | 'mcp_tools' | 'schema' | string;
  created_at?: string;
  /** B4: canonical timestamp. */
  discovered_at?: string;
  /** B4: canonical location of paths/tools/tables. */
  payload?: unknown;
  result?: unknown;
  data?: unknown;
  paths?: unknown;
  tools?: unknown;
  tables?: unknown;
}

export interface ConnectionInvokeResult {
  result: unknown;
  truncated: boolean;
  duration_ms: number;
}

/** Egress rule fields are not fixed by the contract (SECURITY_MODEL section 6); see frontend/README.md. */
export interface EgressRules {
  allowed_schemes?: string[];
  allowed_ports?: number[];
  allowed_hosts?: string[];
  allowed_cidrs?: string[];
  allow_private?: boolean;
  allow_redirects?: boolean;
  max_response_bytes?: number;
  timeout_seconds?: number;
}

export interface EgressPolicy extends EgressRules {
  id: string;
  name: string;
  description?: string | null;
  row_version: number;
}

export interface EgressPolicyInput extends EgressRules {
  name: string;
  description?: string;
}
