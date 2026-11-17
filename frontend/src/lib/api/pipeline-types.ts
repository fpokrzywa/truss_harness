/** Phase 6 shapes (docs/api/PHASE_6_CONTRACT.md). */
import type { JsonSchema } from './integrate-types';
import type { Run } from './operate-types';

export type NodeCategory = 'source' | 'logic' | 'ai' | 'action' | 'control';
export type NodeMaturity = 'validated' | 'experimental' | 'planned';

export interface PortDef {
  name: string;
  schema?: unknown;
  /** Defaults to true on the server. */
  required?: boolean;
}

/** One entry of `GET /api/v1/pipelines/node-types` (the registry; no fake nodes). */
export interface NodeTypeDef {
  type: string;
  label: string;
  category: NodeCategory | string;
  description?: string;
  config_schema?: JsonSchema;
  inputs: PortDef[];
  outputs: PortDef[];
  /** Input ports are named by the node itself (placeholders / expression), not declared by the type. */
  dynamic_inputs?: boolean;
  side_effect: boolean;
  enabled: boolean;
  maturity: NodeMaturity;
  reason_disabled?: string | null;
}

export interface NodeTypeCatalog {
  items: NodeTypeDef[];
}

export type OnFailure = 'fail_fast' | 'continue';

export interface PipelinePolicies {
  max_parallel: number;
  deadline_seconds: number;
  on_failure: OnFailure;
}

export interface PipelineNode {
  id: string;
  /** Registry type key (open set: P7 adds `ontology_query`). */
  type: string;
  config: Record<string, unknown>;
  /** input port name -> "node.output" (or "$params.name"). */
  inputs: Record<string, string>;
  retry?: { max_attempts: number };
  [extra: string]: unknown;
}

/** Edge condition (contract amendment E1): the server accepts only these strings; missing/null = always. */
export type EdgeWhenValue = 'true' | 'false' | 'always';

export interface PipelineEdge {
  from: string;
  to: string;
  /** `"true" | "false" | "always"`. Legacy booleans are read tolerantly; other shapes are preserved untouched. */
  when?: unknown;
}

export interface Position {
  x: number;
  y: number;
}

export interface PipelineSpec {
  schema_version: string;
  params: Record<string, unknown>;
  policies: PipelinePolicies;
  nodes: PipelineNode[];
  edges: PipelineEdge[];
  schemas: Record<string, unknown>;
  /** Presentation only. */
  ui?: { positions?: Record<string, Position> };
  [extra: string]: unknown;
}

/**
 * `outcome_unknown` is never a server status: the server sends `failed` or `waiting_for_approval` plus
 * the node's own `outcome_unknown` flag, and the UI maps the flag to this display status.
 */
export type NodeRunStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'cancelled'
  | 'waiting_for_approval'
  | 'outcome_unknown';

export interface NodeRun {
  node_id: string;
  type: string;
  status: NodeRunStatus;
  attempts: number;
  started_at?: string | null;
  finished_at?: string | null;
  /** Redacted, at most 4 KiB. */
  output_preview?: unknown;
  /** Artifact id string (amendment E1); `{artifact_id|id}` objects are tolerated. */
  output_ref?: unknown;
  error_class?: string | null;
  /** True while this node has an open unknown-write decision (R1.4); the server sends `failed` or `waiting_for_approval` as its status. */
  outcome_unknown?: boolean;
}

/** Pipeline runs are ordinary runs with kind "pipeline". */
export type PipelineRun = Run;

export interface CreatePipelineRunInput {
  kind: 'pipeline';
  pipeline_id: string;
  /** Published version id, or "draft" (test runs). */
  version_id: string;
  workspace: string;
  project: string;
  params: Record<string, unknown>;
  is_test?: boolean;
}

export interface PipelineSchedule {
  id?: string;
  cron?: string;
  timezone?: string;
  next_run_at?: string | null;
  /** Server value: `active | paused`. */
  status?: string;
  /** Legacy; the server sends `status`. */
  enabled?: boolean;
  [k: string]: unknown;
}

/** `GET .../runs/{id}/artifacts/{artifact_id}`: the stored document is wrapped in an envelope. */
export interface RunArtifact {
  id: string;
  run_id: string;
  kind: string;
  size_bytes: number;
  created_at: string;
  truncated?: boolean;
  redacted?: boolean;
  content?: unknown;
}
