/** Phase 5 shapes (docs/api/PHASE_5_CONTRACT.md sections 2-4). */

export type RunStatus =
  | 'pending'
  | 'running'
  | 'waiting_for_approval'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'timed_out';
export type RunKind = 'agent' | 'orchestration' | 'pipeline';

export interface Principal {
  type: string;
  id: string;
  display?: string | null;
}
export interface ScopeRef {
  id: string;
  slug: string;
}
export interface RunUsage {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  model_calls: number;
  tool_calls: number;
}

export interface Run {
  id: string;
  kind: RunKind;
  status: RunStatus;
  tenant_id: string;
  workspace: ScopeRef;
  project: ScopeRef;
  initiated_by: Principal;
  agent_version_id?: string | null;
  prompt_version_id?: string | null;
  orchestration_version_id?: string | null;
  pipeline_id?: string | null;
  pipeline_version_id?: string | null;
  is_test?: boolean;
  parent_run_id?: string | null;
  trace_id: string;
  correlation_id: string;
  idempotency_key?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  deadline_at?: string | null;
  error_class?: ErrorClass | string | null;
  error_message?: string | null;
  attempt_count: number;
  usage: RunUsage;
  waiting_reason?: string | null;
  /** True while a delivered write has no recorded outcome (R1). Shown regardless of `status`. */
  outcome_unknown: boolean;
}

export type StepKind = 'model_call' | 'tool_call' | 'approval' | 'sub_agent' | 'orchestration_node';
export type StepStatus =
  'pending' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'cancelled' | 'indeterminate';

export interface Step {
  id: string;
  run_id: string;
  seq: number;
  kind: StepKind;
  name: string;
  status: StepStatus;
  attempt: number;
  started_at?: string | null;
  finished_at?: string | null;
  model_id?: string | null;
  tool_version_id?: string | null;
  tool_invocation_id?: string | null;
  idempotency_key?: string | null;
  error_class?: string | null;
  usage?: Partial<RunUsage> & Record<string, unknown>;
  input_ref?: unknown;
  output_ref?: unknown;
}

export type RunEventType =
  | 'state_change'
  | 'step_started'
  | 'step_finished'
  | 'gate_decision'
  | 'approval_requested'
  | 'approval_decided'
  | 'retry_scheduled'
  | 'lease_lost'
  | 'recovered'
  | 'cancel_requested';

export interface RunEvent {
  seq: number;
  at: string;
  type: RunEventType | string;
  from?: string | null;
  to?: string | null;
  step_id?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface RunResult {
  output?: unknown;
  /** Some servers return the final text directly. */
  text?: string;
  redacted?: boolean;
  [k: string]: unknown;
}

export interface RunFilters {
  status?: string;
  kind?: string;
  agent_id?: string;
  workspace?: string;
  project?: string;
  initiated_by?: string;
  from?: string;
  to?: string;
  /** `'true'` lists only runs with an open unknown-outcome decision. */
  outcome_unknown?: string;
}

export interface CreateRunInput {
  kind: 'agent';
  agent_id: string;
  version_id?: string;
  workspace: string;
  project: string;
  input: string;
  variables?: Record<string, unknown>;
}

export type ErrorClass =
  | 'validation'
  | 'authz_denied'
  | 'budget_denied'
  | 'provider_unavailable'
  | 'rate_limited'
  | 'timeout'
  | 'transient_network'
  | 'provider_error_4xx'
  | 'tool_error'
  | 'schema_violation'
  | 'approval_rejected'
  | 'cancelled'
  | 'indeterminate_side_effect'
  | 'internal';

export type ApprovalReason =
  'tool_side_effect' | 'approval_rule' | 'orchestration_node' | 'indeterminate_side_effect';
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired' | 'decided';
export type ApprovalDecision = 'approve' | 'reject' | 'mark_succeeded' | 'retry' | 'fail';

export interface ApprovalSummary {
  id: string;
  run_id: string;
  step_id?: string | null;
  reason: ApprovalReason;
  tool?: { key: string; version?: number | string; side_effect_class?: string } | null;
  requested_by?: Principal | string | null;
  requested_at: string;
  expires_at?: string | null;
  status: ApprovalStatus;
}
export interface Approval extends ApprovalSummary {
  /** Exact validated arguments (secrets redacted by the server). */
  arguments?: unknown;
  decision?: {
    decision?: string;
    comment?: string | null;
    decided_by?: Principal | string | null;
    decided_at?: string | null;
  } | null;
  /** Initiator of the run, when the server includes it (separation of duties). */
  run_initiated_by?: Principal | null;
}

// ---- Orchestrations ------------------------------------------------------------------------

export type OrchNodeType = 'agent' | 'tool' | 'condition' | 'approval' | 'parallel';
export type EdgeWhen = 'true' | 'false' | 'always';

export interface OrchNode {
  id: string;
  type: OrchNodeType;
  ref?: { agent_version_id?: string; tool_version_id?: string };
  input_map: Record<string, unknown>;
  condition?: string;
  branches?: string[];
  max_parallel?: number;
}
export interface OrchEdge {
  from: string;
  to: string;
  when?: EdgeWhen;
}
export interface OrchLimits {
  max_nodes_executed: number;
  max_depth: number;
  max_total_tokens: number;
  deadline_s: number;
  max_parallel: number;
}
export interface OrchestrationSpec {
  nodes: OrchNode[];
  edges: OrchEdge[];
  limits: OrchLimits;
}
