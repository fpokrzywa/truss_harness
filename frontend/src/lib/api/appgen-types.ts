/** App requests and drafts of the solution generator (backend `shifde.appgen.schemas`). */

import type { CaseAssertion } from './evaluation-types';

export const REQUEST_STATUSES = [
  'queued',
  'drafting',
  'drafted',
  'failed',
  'accepting',
  'accepted',
  'discarded',
] as const;

export type RequestStatus = (typeof REQUEST_STATUSES)[number];

export interface AppRequestCreate {
  connection_id: string;
  tables: string[];
  brief: string;
  model_id: string;
}

// ---- The draft (design 5.3) --------------------------------------------------------------

export interface DraftAgent {
  name: string;
  instructions: string;
}

export const PARAMETER_TYPES = ['string', 'integer', 'number', 'boolean', 'date'] as const;

export type ParameterType = (typeof PARAMETER_TYPES)[number];

export interface DraftParameter {
  name: string;
  type: ParameterType;
  required: boolean;
}

export interface DraftTool {
  name: string;
  description: string;
  sql: string;
  parameters: DraftParameter[];
  max_rows: number;
}

/** The screens of a generated app (`packaging.schemas.Screen`, `@shifde/appgen-web`'s `Screen`). */
export interface ChatScreen {
  kind: 'chat';
  title: string;
  placeholder?: string;
}

export interface TableScreen {
  kind: 'table';
  title: string;
  tool: string;
  inputs: string[];
}

export type Screen = ChatScreen | TableScreen;

export interface DraftCase {
  case_id: string;
  input: { message: string };
  assertions: CaseAssertion[];
}

export interface Draft {
  agent: DraftAgent;
  tools: DraftTool[];
  screens: Screen[];
  evaluation_cases: DraftCase[];
}

export interface DraftEdit {
  base_revision_no: number;
  draft: Draft;
}

/** One diagnostic of a draft (design 5.4); `path` is dotted, as `tools.0.sql`. */
export interface DraftDiagnostic {
  path: string;
  code: string;
  message: string;
  severity: 'error' | 'warning';
}

export interface DraftOut {
  revision_no: number;
  source: 'model' | 'edited';
  draft: Draft;
  draft_hash: string;
  validation: DraftDiagnostic[];
  created_by: string;
  created_at: string;
}

// ---- Requests (design section 7) ---------------------------------------------------------

export interface AppRequestSummary {
  id: string;
  workspace_id: string;
  project_id: string;
  connection_id: string;
  status: RequestStatus;
  status_reason: string | null;
  latest_revision_no: number | null;
  solution_version_id: string | null;
  requested_by: string;
  created_at: string;
  updated_at: string | null;
}

export interface AppRequestOut extends AppRequestSummary {
  tables: string[];
  brief: string;
  model_id: string;
  discovery_id: string;
  accepted_revision_no: number | null;
  /** The accept steps done so far, as `solution_id` and `solution_version_id`. */
  accept_progress: Record<string, unknown>;
  revisions: DraftOut[];
}

/** `min_accuracy` and `max_total_cost_usd` are decimal strings. */
export interface AcceptThresholds {
  min_accuracy: string;
  max_p95_latency_ms: number;
  max_total_cost_usd: string;
}

export interface AcceptIn {
  revision_no: number;
  solution_name: string;
  model_id: string;
  thresholds: AcceptThresholds;
}

// ---- App preview of a release (design section 7) ----------------------------------------

/** One rendered file of the app: metadata only, never its content. */
export interface AppPreviewFile {
  path: string;
  size: number;
  sha256: string;
}

export interface AppPreviewTool {
  name: string;
  description: string;
  sql: string;
  parameters: { name: string; type: string; required: boolean }[];
  max_rows: number;
}

export interface AppPreviewOut {
  release_id: string;
  release_digest: string;
  generator_version: string;
  files: AppPreviewFile[];
  /** Screen-shaped documents; parse with `@shifde/appgen-web`'s `parseScreens` before drawing. */
  screens: unknown[];
  tools: AppPreviewTool[];
}
