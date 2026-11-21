/** Evaluation datasets and evaluation runs (backend `shifde.evaluation.schemas`). */

export type AssertionKind = 'equals' | 'contains' | 'not_contains' | 'regex' | 'json_equals';

export interface CaseAssertion {
  id: string;
  kind: AssertionKind;
  value: unknown;
}

export interface EvaluationCase {
  case_id: string;
  input: Record<string, unknown>;
  assertions: CaseAssertion[];
}

export interface DatasetCreate {
  name: string;
  cases: EvaluationCase[];
}

export interface DatasetOut {
  id: string;
  workspace: string | null;
  project: string | null;
  name: string;
  version_no: number;
  cases: Record<string, unknown>[];
  digest: string;
  created_by: string;
  created_at: string;
}

export type EvaluationStatus = 'queued' | 'running' | 'passed' | 'failed' | 'error';

export interface AssertionResult {
  id: string;
  passed: boolean;
  reason: string | null;
}

/** One case's score; never the case's output text. */
export interface CaseResult {
  case_id: string;
  run_id: string;
  passed: boolean;
  assertions: AssertionResult[];
  latency_ms: number;
  cost_usd: string;
}

export interface EvaluationThresholds {
  min_accuracy?: string;
  max_p95_latency_ms?: number;
  max_total_cost_usd?: string;
}

/** `accuracy` and `total_cost_usd` are decimal strings; result fields are null until scored. */
export interface EvaluationRunOut {
  id: string;
  release_id: string;
  release_digest: string;
  dataset_id: string;
  dataset_digest: string;
  thresholds: EvaluationThresholds;
  status: EvaluationStatus;
  status_reason: string | null;
  accuracy: string | null;
  p95_latency_ms: number | null;
  total_cost_usd: string | null;
  results: CaseResult[] | null;
  requested_by: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}
