/** Phase 7 shapes (docs/api/PHASE_7_CONTRACT.md sections 1-4). */

export const PROPERTY_TYPES = [
  'string',
  'integer',
  'number',
  'boolean',
  'date',
  'datetime',
  'json',
] as const;
export type PropertyType = (typeof PROPERTY_TYPES)[number];

export const CLASSIFICATIONS = ['public', 'internal', 'confidential', 'restricted'] as const;
export type Classification = (typeof CLASSIFICATIONS)[number];

export const CARDINALITIES = ['one_to_one', 'one_to_many', 'many_to_one', 'many_to_many'] as const;
export type Cardinality = (typeof CARDINALITIES)[number];

export interface Namespace {
  key: string;
  label: string;
}

export interface OntProperty {
  key: string;
  type: PropertyType;
  nullable: boolean;
  classification: Classification;
  label?: string;
}

export interface ObjectType {
  key: string;
  label: string;
  namespace: string;
  properties: OntProperty[];
  primary_key: string[];
  display_property?: string;
}

export interface Relationship {
  key: string;
  label: string;
  from: string;
  to: string;
  cardinality: Cardinality;
  constraints: { required: boolean };
}

export interface OntAction {
  key: string;
  object_type: string;
  tool_version_id: string;
  param_mapping: Record<string, string>;
}

export interface Position {
  x: number;
  y: number;
}

export interface OntologySpec {
  namespaces: Namespace[];
  object_types: ObjectType[];
  relationships: Relationship[];
  actions: OntAction[];
  ui: { positions: Record<string, Position> };
}

export type MappingTargetKind = 'object_type' | 'relationship';

export interface RelationRef {
  schema: string;
  name: string;
}

/** Relationship key mapping: direct from/to columns, or a link table (with its own relation). */
export interface KeyMap {
  relation?: RelationRef;
  from_column: string;
  to_column: string;
}

export interface Mapping {
  target_kind: MappingTargetKind;
  target_key: string;
  connection_id: string;
  relation: RelationRef;
  column_map: Record<string, string>;
  key_map?: KeyMap;
  filters: string[];
  status?: string;
}

export interface MappingIssue {
  code?: string;
  message: string;
  path?: string;
  /** Additive: absent on older servers (treated as `error`). */
  severity?: 'error' | 'warning';
}

export interface MappingCheck {
  ok: boolean;
  issues: MappingIssue[];
  sample_columns?: { name: string; type?: string }[];
}

export interface PolicySubject {
  type: 'role' | 'group' | 'user';
  id: string;
}

export interface Policy {
  id: string;
  object_type_key: string;
  /** null = every NON-restricted property; restricted properties must be named explicitly. */
  property_keys: string[] | null;
  subject: PolicySubject;
  effect: 'allow';
  row_filter?: string | null;
  row_version?: number;
}

export interface PolicyInput {
  object_type_key: string;
  property_keys: string[] | null;
  subject: PolicySubject;
  effect: 'allow';
  row_filter?: string;
}

export interface TraverseHop {
  relationship: string;
  /** Contract F1: `reverse` follows the relationship from its `to` end; default forward. */
  direction: 'forward' | 'reverse';
  select: string[];
  filter?: string;
}

export interface OntologyQuery {
  version_id?: string;
  object_type: string;
  select: string[];
  filter?: string;
  traverse?: TraverseHop[];
  limit: number;
  cursor?: string;
}

export type OntRow = Record<string, unknown>;

export interface OntologyQueryResult {
  rows: OntRow[];
  next_cursor?: string | null;
  lineage_id: string;
  /** A related-row cap was reached while traversing. */
  truncated?: boolean;
}

/** The contract leaves the shape open: counts may be a map or a list; see normalizeObjectView. */
export interface RelationshipSummary {
  relationship: string;
  count: number;
  /** The related list was truncated: `count` is a lower bound. */
  capped?: boolean;
  label?: string;
  direction?: 'forward' | 'reverse';
}

export interface ObjectView {
  properties: OntRow;
  relationships: RelationshipSummary[];
}

export interface LineageRecord {
  id: string;
  at: string;
  principal?: unknown;
  run_id?: string | null;
  ontology_version_id?: string;
  channel?: string;
  outcome?: string;
  error_code?: string | null;
  object_types?: string[];
  connections?: string[];
  compiled_query_hashes?: string[];
  row_counts?: unknown;
  duration_ms?: number;
}

export interface ActionInvokeInput {
  object_pk: string;
  extra_params?: Record<string, unknown>;
  /** Slugs; required by the server unless the ontology is project-scoped (else 422). */
  workspace?: string;
  project?: string;
}

export type PrincipalType = 'user' | 'service_account';

/** Attribute map used by policy row filters (`principal.attributes.*`). */
export interface PrincipalAttributes {
  principal_type: PrincipalType;
  principal_id: string;
  attributes: Record<string, unknown>;
  row_version: number;
  updated_at?: string;
}
