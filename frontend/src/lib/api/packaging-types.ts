/** Solutions, solution versions and releases (backend `shifde.packaging.schemas`). */

export const TARGET_KIND = 'databricks.app/v1';

export type SolutionStatus = 'active' | 'archived';

export interface SolutionCreate {
  name: string;
  target_kind: typeof TARGET_KIND;
}

export interface SolutionOut {
  id: string;
  workspace: string | null;
  project: string | null;
  name: string;
  target_kind: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string | null;
}

export interface DataRead {
  connection_id: string;
  tables: string[];
}

/** `min_accuracy` and `max_total_cost_usd` are decimal strings. */
export interface EvaluationContract {
  dataset_id: string;
  min_accuracy: string;
  max_p95_latency_ms: number;
  max_total_cost_usd: string;
}

export type EntrypointKind = 'agent' | 'pipeline';

export interface SolutionVersionCreate {
  entrypoint_kind: EntrypointKind;
  entrypoint_version_id: string;
  data_reads: DataRead[];
  evaluation: EvaluationContract | null;
}

/** `version_id` and `spec_hash` are null for a version the caller cannot see. */
export interface EntrypointOut {
  kind: string;
  version_id: string | null;
  spec_hash: string | null;
}

export interface SolutionPinOut {
  role: string;
  version_id: string | null;
  spec_hash: string | null;
}

export interface SolutionVersionOut {
  id: string;
  solution_id: string;
  version_no: number;
  entrypoint: EntrypointOut;
  spec: Record<string, unknown>;
  spec_hash: string;
  pins: SolutionPinOut[];
  created_by: string;
  created_at: string;
}

// ---- Releases ----------------------------------------------------------------------------

export interface ModelBinding {
  provider_id: string;
  model: string;
}

/** Keys are pin names: a model id (or `route-<route id>`) and a connection id. */
export interface EnvironmentBindings {
  model_bindings: Record<string, ModelBinding>;
  connection_bindings: Record<string, string>;
}

export interface ReleaseCreate {
  environment_bindings: EnvironmentBindings;
}

/** One feature of the compatibility check; `reason` is null when it is supported. */
export interface Diagnostic {
  path: string;
  status: 'supported' | 'unsupported';
  reason: string | null;
  mandatory?: boolean;
}

export type ReleaseStatus = 'sealed' | 'withdrawn';

export interface ReleaseOut {
  id: string;
  solution_id: string;
  solution_version_id: string;
  version_no: number;
  workspace: string | null;
  project: string | null;
  digest: string;
  status: string;
  manifest: Record<string, unknown>;
  compatibility: Diagnostic[];
  sealed_by: string;
  sealed_at: string;
  updated_at: string | null;
}
