/** Ask SETH: settings, catalog, specialists, requests, proposals (backend `shifde.assist.schemas`). */

import type { RunStatus } from './operate-types';

// ---- Settings and specialists ------------------------------------------------------------

export interface AssistSettingsOut {
  assist_enabled: boolean;
  /** The pinned catalog version; `current_catalog_version` is the one install accepts. */
  catalog_version: string | null;
  current_catalog_version: string;
  model_routing_enabled: boolean;
  router_model_id: string | null;
  specialist_max_cost: number;
  updated_at: string | null;
}

export interface AssistSettingsPut {
  model_routing_enabled: boolean;
  router_model_id: string | null;
  specialist_max_cost: number;
}

export interface AssistInstallIn {
  model_id: string;
  keys?: string[];
}

export type SpecialistStatus = 'installing' | 'enabled' | 'disabled';

export interface SpecialistOut {
  id: string;
  key: string;
  title: string;
  workspace_id: string;
  project_id: string;
  status: SpecialistStatus;
  agent_id: string | null;
  agent_version_id: string | null;
  catalog_version: string;
  /** Enabled here, and on for the organization on the current catalog version. */
  available: boolean;
  installed_by: string;
  created_at: string;
  updated_at: string | null;
}

export interface InstallStatus {
  workspace_id: string;
  project_id: string;
  status: SpecialistStatus;
  available: boolean;
}

export interface CatalogEntryOut {
  key: string;
  title: string;
  router_description: string;
  tools: string[];
  required_permissions: string[];
  proposal_kinds: string[];
  installs: InstallStatus[];
}

export interface AssistCatalogOut {
  catalog_version: string;
  assist_enabled: boolean;
  entries: CatalogEntryOut[];
}

// ---- Requests ----------------------------------------------------------------------------

export const ASSIST_REQUEST_STATUSES = [
  'queued',
  'classifying',
  'handing_over',
  'routed',
  'refused',
  'failed',
] as const;

export type AssistRequestStatus = (typeof ASSIST_REQUEST_STATUSES)[number];

export const CONTEXT_KINDS = [
  'solution',
  'release',
  'target',
  'plan',
  'deployment',
  'evaluation_run',
] as const;

export type ContextKind = (typeof CONTEXT_KINDS)[number];

/** The object a request is about. */
export interface AssistContext {
  kind: ContextKind;
  id: string;
}

export interface AssistRequestIn {
  text: string;
  specialist?: string;
  context?: AssistContext;
}

export type RouteMethod = 'explicit' | 'rule' | 'model';

export interface AssistRequestSummary {
  id: string;
  workspace_id: string;
  project_id: string;
  status: AssistRequestStatus;
  status_reason: string | null;
  requested_specialist: string | null;
  specialist_key: string | null;
  route_method: RouteMethod | null;
  run_id: string | null;
  requested_by: string;
  created_at: string;
  updated_at: string | null;
}

export interface RunRefOut {
  id: string;
  status: RunStatus;
  error_code: string | null;
}

/** A request with its route, run and proposals; never its text. */
export interface AssistRequestOut extends AssistRequestSummary {
  rule_id: string | null;
  router_version: string | null;
  candidates: string[];
  /** Set only on the answer to the request that was refused `specialist_not_permitted`. */
  missing_permissions: string[] | null;
  context: AssistContext | null;
  /** Null when the run is gone or the caller may not read it. */
  run: RunRefOut | null;
  proposals: ProposalOut[];
}

// ---- Proposals ---------------------------------------------------------------------------

export type ProposalKind = 'appgen_request' | 'evaluation_run' | 'delivery_plan';
export type ProposalStatus = 'open' | 'accepted' | 'failed' | 'dismissed';

export interface ProposalOut {
  id: string;
  run_id: string;
  kind: ProposalKind;
  /** Null when the caller may not read what the proposal names. */
  payload: Record<string, unknown> | null;
  status: ProposalStatus;
  status_reason: string | null;
  /** Null until accepted, and when the caller cannot read the created object. */
  result_ref: { kind: ProposalKind; id: string } | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
  updated_at: string | null;
}
