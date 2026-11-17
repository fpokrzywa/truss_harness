/** Phase 4 shapes (docs/api/PHASE_4_CONTRACT.md, incl. amendment C1). */

export type ArtifactKind =
  'prompts' | 'agents' | 'tools' | 'skills' | 'orchestrations' | 'pipelines' | 'ontologies';
export type ArtifactVisibility = 'private' | 'scope' | 'tenant';
export type ArtifactStatus = 'active' | 'disabled' | 'retired';
export type VersionState = 'draft' | 'published' | 'deprecated' | 'retired';

export interface ArtifactScope {
  type: 'tenant' | 'workspace' | 'project';
  id: string;
}

export interface VersionRef {
  id: string;
  version_no: number;
}

export interface Artifact {
  id: string;
  key: string;
  name: string;
  description?: string | null;
  scope: ArtifactScope;
  visibility: ArtifactVisibility;
  status: ArtifactStatus;
  latest_published_version: VersionRef | null;
  has_draft: boolean;
  owner?: unknown;
  row_version: number;
  created_at?: string;
  updated_at?: string;
}

export interface ArtifactVersion {
  id: string;
  version_no: number;
  state: VersionState;
  /** Absent in the versions list. */
  spec?: Record<string, unknown>;
  spec_schema_version?: number | string;
  spec_hash?: string | null;
  change_note?: string | null;
  created_by?: unknown;
  created_at?: string;
  published_by?: unknown;
  published_at?: string | null;
  based_on_version_id?: string | null;
  /** Needed for draft saves; read from the draft GET (see README Decisions). */
  row_version?: number;
}

export interface CreateArtifactInput {
  key: string;
  name: string;
  description?: string;
  scope: ArtifactScope;
  visibility: ArtifactVisibility;
  spec?: Record<string, unknown>;
}

export interface ArtifactFilters {
  scope_type?: string;
  scope_id?: string;
  status?: string;
  q?: string;
}

export interface ValidationIssue {
  path: string;
  code?: string;
  message: string;
  /** Pipelines (P6): the node or edge the issue belongs to. */
  node_id?: string;
  edge?: { from: string; to: string };
  /** Ontologies (P7): the object type or relationship key the issue belongs to. */
  key?: string;
}
export interface ValidationResult {
  valid: boolean;
  errors: ValidationIssue[];
  warnings?: ValidationIssue[];
  /** Pipelines (P6): execution order computed by the server. */
  topo_order?: string[];
}

export interface DiffChange {
  path: string;
  op: 'add' | 'remove' | 'replace';
  from?: unknown;
  to?: unknown;
}

export interface PublishRequest {
  id: string;
  artifact: { kind: ArtifactKind; id: string; key: string; name: string };
  version_id: string;
  version_no: number;
  change_note: string | null;
  requested_by: { id: string; display: string };
  requested_at: string;
  status: 'pending' | 'approved' | 'rejected';
  decided_by?: { id: string; display: string } | null;
  decided_at?: string | null;
  comment?: string | null;
}

/** 200/201 returns the published Version; 202 returns `{publish_request}` (approval required). */
export type PublishResult =
  { kind: 'published'; version: ArtifactVersion } | { kind: 'pending'; request: PublishRequest };

// ---- Prompts -------------------------------------------------------------------------------

export type VariableType = 'string' | 'number' | 'boolean' | 'json' | 'text';
export interface PromptVariable {
  name: string;
  type: VariableType;
  required: boolean;
  json_schema?: unknown;
  default?: unknown;
  trusted: boolean;
  max_length?: number;
}
export interface PromptExample {
  inputs: Record<string, unknown>;
  output: string;
}
export interface PromptComposition {
  prompt_version_id: string;
  placement: 'system_prefix' | 'context_prefix';
}
export type CriterionType = 'contains' | 'not_contains' | 'regex' | 'json_schema' | 'max_length';
export interface EvaluationCriterion {
  type: CriterionType;
  value: string | number;
  name: string;
}
export interface PromptSpec {
  sections: { system: string; context?: string; constraints?: string; output_format?: string };
  variables: PromptVariable[];
  examples: PromptExample[];
  composed_of: PromptComposition[];
  default_model_id?: string;
  evaluation_criteria: EvaluationCriterion[];
}

export interface RenderedMessage {
  role: string;
  content: string;
}
export interface RenderResult {
  messages: RenderedMessage[];
  warnings?: string[];
}
export interface CriterionResult {
  name: string;
  passed: boolean;
  detail?: string | null;
}
export interface PromptTestRun {
  id: string;
  output: string;
  usage: { input_tokens: number; output_tokens: number };
  latency_ms: number;
  evaluation: CriterionResult[];
  verdict: 'pass' | 'fail';
  created_at?: string;
  version_id?: string | null;
  model_id?: string | null;
}

// ---- Tools ---------------------------------------------------------------------------------

export type ToolExecutor = 'builtin.function' | 'rest.http' | 'mcp.tool' | 'connection.sql';
export type SideEffectClass =
  'none' | 'read' | 'write_idempotent' | 'write_non_idempotent' | 'destructive';
export type Idempotency = 'none' | 'key_supported';

export interface ToolSpec {
  executor: ToolExecutor;
  binding: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  side_effect_class: SideEffectClass;
  required_permissions: string[];
  invocation_policy: { max_calls_per_run?: number; allowed_agent_ids?: string[] };
  timeout_s: number;
  retry: { max_attempts: number; backoff_s: number };
  audit_level: 'metadata' | 'full';
  idempotency: Idempotency;
}

export interface ExecutorFunction {
  key: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  side_effect_class?: SideEffectClass;
}
export interface ExecutorDescriptor {
  key: ToolExecutor | string;
  label?: string;
  description?: string;
  maturity?: 'planned' | 'experimental' | 'validated';
  functions?: ExecutorFunction[];
}
export interface ExecutorCatalog {
  items: ExecutorDescriptor[];
}

export interface ToolTestResult {
  output: unknown;
  valid_output: boolean;
  duration_ms: number;
  attempts: number;
}

// ---- Skills --------------------------------------------------------------------------------

export interface SkillInput {
  name: string;
  description: string;
  type: string;
}
export interface SkillSpec {
  instructions_md: string;
  required_inputs: SkillInput[];
  dependencies: { skill_version_id: string }[];
  applicable_tools: string[];
  validation: { max_instruction_chars: number };
}

// ---- Agents --------------------------------------------------------------------------------

export interface AgentSpec {
  /** Pinned version id, or `latest:{artifact_id}` in drafts (resolved and pinned at publish). */
  prompt_version_id: string;
  model: { model_id: string; fallback_model_ids: string[] } | { route_id: string };
  tool_version_ids: string[];
  skill_version_ids: string[];
  memory: { mode: 'none' | 'run' | 'session'; max_turns: number };
  limits: {
    max_iterations: number;
    max_tool_calls: number;
    max_tokens: number;
    deadline_s: number;
    max_cost?: number;
  };
  execution_permissions: {
    allowed_principal_types: ('user' | 'service_account')[];
    required_permission: string;
  };
  approval_rules: { require_approval_for: SideEffectClass[] };
  variables_map: Record<string, string>;
}

export interface AgentTestResult {
  assembled: {
    messages: RenderedMessage[];
    tools: unknown[];
  };
  output: string;
  usage: { input_tokens: number; output_tokens: number };
}

export interface AgentResolved {
  spec_hash?: string | null;
  prompt_version?: unknown;
  tools?: unknown[];
  skills?: unknown[];
  models?: unknown[];
  [k: string]: unknown;
}
