import type { ValidationIssue } from './api/build-types';
import type {
  Cardinality,
  Mapping,
  MappingTargetKind,
  ObjectType,
  ObjectView,
  OntAction,
  OntologySpec,
  OntProperty,
  OntRow,
  Position,
  PropertyType,
  Relationship,
  RelationshipSummary,
} from './api/ontology-types';
import { checkExpression, type ExprResult } from './expr';

export const KEY_RE = /^[a-z0-9][a-z0-9._-]*$/;
export const PROP_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
export const MAX_TRAVERSE_DEPTH = 3;
export const MAX_LIMIT = 1000;

export function emptyOntologySpec(): OntologySpec {
  return {
    namespaces: [],
    object_types: [],
    relationships: [],
    actions: [],
    ui: { positions: {} },
  };
}

export function normalizeOntologySpec(raw: Record<string, unknown> | undefined): OntologySpec {
  const d = emptyOntologySpec();
  if (!raw) return d;
  const r = raw as Partial<OntologySpec> & Record<string, unknown>;
  return {
    ...r,
    namespaces: r.namespaces ?? [],
    object_types: (r.object_types ?? []).map((o) => ({
      ...o,
      properties: (o.properties ?? []).map((p) => ({
        ...p,
        nullable: p.nullable ?? false,
        classification: p.classification ?? 'internal',
      })),
      primary_key: o.primary_key ?? [],
    })),
    relationships: (r.relationships ?? []).map((x) => ({
      ...x,
      constraints: { required: x.constraints?.required ?? false },
    })),
    actions: r.actions ?? [],
    ui: { ...(r.ui ?? {}), positions: r.ui?.positions ?? {} },
  };
}

export const edgeKey = (rel: Pick<Relationship, 'key'>) => rel.key;

// ---- Graph edits (pure) ---------------------------------------------------------------------

export function addNamespace(spec: OntologySpec, key: string, label: string): OntologySpec {
  return { ...spec, namespaces: [...spec.namespaces, { key, label: label || key }] };
}

export function removeNamespace(spec: OntologySpec, key: string): OntologySpec {
  return { ...spec, namespaces: spec.namespaces.filter((n) => n.key !== key) };
}

export function namespaceInUse(spec: OntologySpec, key: string): boolean {
  return spec.object_types.some((o) => o.namespace === key);
}

/** Inserts or replaces by key. */
export function upsertObjectType(spec: OntologySpec, ot: ObjectType): OntologySpec {
  const exists = spec.object_types.some((o) => o.key === ot.key);
  return {
    ...spec,
    object_types: exists
      ? spec.object_types.map((o) => (o.key === ot.key ? ot : o))
      : [...spec.object_types, ot],
  };
}

/** Removes the object type, the relationships touching it, its actions and its position. */
export function removeObjectType(spec: OntologySpec, key: string): OntologySpec {
  const { [key]: _drop, ...positions } = spec.ui.positions;
  void _drop;
  return {
    ...spec,
    object_types: spec.object_types.filter((o) => o.key !== key),
    relationships: spec.relationships.filter((r) => r.from !== key && r.to !== key),
    actions: spec.actions.filter((a) => a.object_type !== key),
    ui: { ...spec.ui, positions },
  };
}

export function upsertAction(spec: OntologySpec, a: OntAction): OntologySpec {
  const exists = spec.actions.some((x) => x.key === a.key);
  return {
    ...spec,
    actions: exists ? spec.actions.map((x) => (x.key === a.key ? a : x)) : [...spec.actions, a],
  };
}

export function removeAction(spec: OntologySpec, key: string): OntologySpec {
  return { ...spec, actions: spec.actions.filter((a) => a.key !== key) };
}

export function upsertRelationship(spec: OntologySpec, rel: Relationship): OntologySpec {
  const exists = spec.relationships.some((r) => r.key === rel.key);
  return {
    ...spec,
    relationships: exists
      ? spec.relationships.map((r) => (r.key === rel.key ? rel : r))
      : [...spec.relationships, rel],
  };
}

export function removeRelationship(spec: OntologySpec, key: string): OntologySpec {
  return { ...spec, relationships: spec.relationships.filter((r) => r.key !== key) };
}

/** Positions are rounded so the spec does not churn on sub-pixel drags. */
export function setPosition(spec: OntologySpec, key: string, pos: Position): OntologySpec {
  return {
    ...spec,
    ui: {
      ...spec.ui,
      positions: {
        ...spec.ui.positions,
        [key]: { x: Math.round(pos.x), y: Math.round(pos.y) },
      },
    },
  };
}

/** Saved position, else a simple grid so unpositioned nodes never overlap. */
export function layoutPositions(spec: OntologySpec): Map<string, Position> {
  const out = new Map<string, Position>();
  spec.object_types.forEach((o, i) => {
    out.set(o.key, spec.ui.positions[o.key] ?? { x: (i % 3) * 320, y: Math.floor(i / 3) * 300 });
  });
  return out;
}

export function newProperty(spec?: ObjectType): OntProperty {
  const used = new Set(spec?.properties.map((p) => p.key));
  let i = 1;
  while (used.has(`property_${i}`)) i++;
  return {
    key: `property_${i}`,
    type: 'string',
    nullable: true,
    classification: 'internal',
  };
}

export const CARDINALITY_LABEL: Record<Cardinality, string> = {
  one_to_one: '1:1',
  one_to_many: '1:N',
  many_to_one: 'N:1',
  many_to_many: 'N:N',
};

export const cardinalityText = (c: Cardinality): string =>
  ({
    one_to_one: 'One to one',
    one_to_many: 'One to many',
    many_to_one: 'Many to one',
    many_to_many: 'Many to many',
  })[c];

export function relationshipEdgeLabel(r: Relationship): string {
  return `${r.label} (${r.key}) ${CARDINALITY_LABEL[r.cardinality]}${r.constraints.required ? ' required' : ''}`;
}

// ---- Client-side checks ---------------------------------------------------------------------

export interface OntologyIssue {
  code: string;
  message: string;
  severity: 'error' | 'warning';
  /** Blocking issues disable save/publish; the server is authoritative for the rest. */
  blocking: boolean;
  objectKey?: string;
  relationshipKey?: string;
}

function dupes(keys: string[]): Set<string> {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const k of keys) (seen.has(k) ? dup : seen).add(k);
  return dup;
}

/** Mirrors the contract section 1 rules that can be decided without mappings or tool lookups. */
export function ontologyIssues(spec: OntologySpec): OntologyIssue[] {
  const out: OntologyIssue[] = [];
  const nsKeys = new Set(spec.namespaces.map((n) => n.key));
  const typeKeys = new Set(spec.object_types.map((o) => o.key));

  for (const k of dupes([
    ...spec.namespaces.map((n) => n.key),
    ...spec.object_types.map((o) => o.key),
    ...spec.relationships.map((r) => r.key),
  ]))
    out.push({
      code: 'duplicate_key',
      message: `Key "${k}" is used more than once.`,
      severity: 'error',
      blocking: true,
      objectKey: typeKeys.has(k) ? k : undefined,
      relationshipKey: spec.relationships.some((r) => r.key === k) ? k : undefined,
    });

  for (const o of spec.object_types) {
    if (!nsKeys.has(o.namespace))
      out.push({
        code: 'unknown_namespace',
        message: `${o.key}: namespace "${o.namespace}" does not exist.`,
        severity: 'error',
        blocking: true,
        objectKey: o.key,
      });
    for (const p of dupes(o.properties.map((x) => x.key)))
      out.push({
        code: 'duplicate_key',
        message: `${o.key}: property "${p}" is defined more than once.`,
        severity: 'error',
        blocking: true,
        objectKey: o.key,
      });
    const props = new Map(o.properties.map((p) => [p.key, p]));
    for (const pk of o.primary_key) {
      const p = props.get(pk);
      if (!p)
        out.push({
          code: 'invalid_primary_key',
          message: `${o.key}: primary key property "${pk}" does not exist.`,
          severity: 'error',
          blocking: true,
          objectKey: o.key,
        });
      else if (p.nullable)
        out.push({
          code: 'invalid_primary_key',
          message: `${o.key}: primary key property "${pk}" must not be nullable.`,
          severity: 'warning',
          blocking: false,
          objectKey: o.key,
        });
    }
    if (o.primary_key.length === 0)
      out.push({
        code: 'invalid_primary_key',
        message: `${o.key}: no primary key chosen yet.`,
        severity: 'warning',
        blocking: false,
        objectKey: o.key,
      });
    if (o.display_property && !props.has(o.display_property))
      out.push({
        code: 'invalid_primary_key',
        message: `${o.key}: display property "${o.display_property}" does not exist.`,
        severity: 'error',
        blocking: true,
        objectKey: o.key,
      });
  }

  for (const r of spec.relationships) {
    for (const end of [r.from, r.to])
      if (!typeKeys.has(end))
        out.push({
          code: 'dangling_relationship',
          message: `${r.key}: object type "${end}" does not exist.`,
          severity: 'error',
          blocking: true,
          relationshipKey: r.key,
        });
  }
  for (const a of spec.actions) {
    const ot = spec.object_types.find((o) => o.key === a.object_type);
    if (!ot) {
      out.push({
        code: 'action_tool_invalid',
        message: `Action ${a.key}: object type "${a.object_type}" does not exist.`,
        severity: 'error',
        blocking: true,
      });
      continue;
    }
    for (const prop of missingMappedProperties(a.param_mapping, ot))
      out.push({
        code: 'action_tool_invalid',
        message: `Action ${a.key}: mapping refers to unknown property "${prop}" of ${ot.key}.`,
        severity: 'error',
        blocking: true,
        objectKey: ot.key,
      });
  }
  return out;
}

export const ontologyBlockers = (spec: OntologySpec): string[] =>
  ontologyIssues(spec)
    .filter((i) => i.blocking)
    .map((i) => i.message);

/**
 * Which node or edge a server validation issue belongs to. Uses `node_id` or `key` when present, else
 * parses `object_types[i]`, `relationships[i]` (index or key in brackets) or `mappings/{kind}/{key}`
 * out of `path`.
 */
export function issueTarget(
  issue: ValidationIssue & { key?: string },
  spec: OntologySpec,
): { objectKey?: string; relationshipKey?: string } {
  const direct = issue.node_id ?? issue.key;
  if (direct) {
    if (spec.object_types.some((o) => o.key === direct)) return { objectKey: direct };
    if (spec.relationships.some((r) => r.key === direct)) return { relationshipKey: direct };
  }
  const path = issue.path ?? '';
  const m = /^(object_types|relationships)\[([^\]]+)\]/.exec(path);
  if (m) {
    const list = m[1] === 'object_types' ? spec.object_types : spec.relationships;
    const idx = /^\d+$/.test(m[2] as string)
      ? list[Number(m[2])]
      : list.find((x) => x.key === m[2]);
    if (idx) return m[1] === 'object_types' ? { objectKey: idx.key } : { relationshipKey: idx.key };
  }
  const mm = /mappings\/(object_type|relationship)\/([^/\s]+)/.exec(path);
  if (mm) {
    const key = decodeURIComponent(mm[2] as string);
    return mm[1] === 'object_type' ? { objectKey: key } : { relationshipKey: key };
  }
  return {};
}

export function validationHighlights(
  errors: readonly ValidationIssue[],
  spec: OntologySpec,
): { objects: Set<string>; relationships: Set<string> } {
  const objects = new Set<string>();
  const relationships = new Set<string>();
  for (const e of errors) {
    const t = issueTarget(e, spec);
    if (t.objectKey) objects.add(t.objectKey);
    if (t.relationshipKey) relationships.add(t.relationshipKey);
  }
  return { objects, relationships };
}

// ---- Type compatibility ----------------------------------------------------------------------

const COLUMN_FAMILIES: Record<PropertyType, string[]> = {
  string: [
    'text',
    'varchar',
    'char',
    'character',
    'character varying',
    'bpchar',
    'citext',
    'uuid',
    'name',
    'string',
    'nvarchar',
    'nchar',
    'clob',
  ],
  integer: [
    'int',
    'int2',
    'int4',
    'int8',
    'integer',
    'smallint',
    'bigint',
    'tinyint',
    'serial',
    'smallserial',
    'bigserial',
    'mediumint',
  ],
  number: [
    'numeric',
    'decimal',
    'float',
    'float4',
    'float8',
    'double',
    'double precision',
    'real',
    'money',
    'number',
  ],
  boolean: ['bool', 'boolean', 'bit'],
  date: ['date'],
  datetime: [
    'timestamp',
    'timestamptz',
    'timestamp with time zone',
    'timestamp without time zone',
    'datetime',
    'datetime2',
    'datetimeoffset',
  ],
  json: ['json', 'jsonb'],
};

function familyOf(columnType: string): PropertyType | null {
  const t = columnType
    .toLowerCase()
    .replace(/\(.*?\)/g, '')
    .trim();
  for (const [family, names] of Object.entries(COLUMN_FAMILIES) as [PropertyType, string[]][])
    if (names.includes(t)) return family;
  if (t.startsWith('timestamp')) return 'datetime';
  if (t.startsWith('int')) return 'integer';
  return null;
}

export type Compat = 'compatible' | 'incompatible' | 'unknown';

/** Hint only; the server enforces `type_incompatible` at publish. Integer columns widen into number. */
export function typeCompat(propType: PropertyType, columnType: string | undefined): Compat {
  if (!columnType) return 'unknown';
  const fam = familyOf(columnType);
  if (!fam) return 'unknown';
  if (fam === propType) return 'compatible';
  if (propType === 'number' && fam === 'integer') return 'compatible';
  return 'incompatible';
}

// ---- Mapping ---------------------------------------------------------------------------------

export interface MappingDraft {
  connectionId: string;
  /** `schema.name` or `name` identifier as shown by the relation picker. */
  relation: { schema: string; name: string };
  columnMap: Record<string, string>;
  filters: string[];
  keyMode: 'direct' | 'link';
  fromColumn: string;
  toColumn: string;
  linkRelation: { schema: string; name: string };
}

export const relationId = (r: { schema?: string; name: string }) =>
  r.schema ? `${r.schema}.${r.name}` : r.name;

/** Builds the PUT body. Unmapped properties (empty column) are omitted; empty filters are dropped. */
export function buildMapping(kind: MappingTargetKind, key: string, d: MappingDraft): Mapping {
  const column_map: Record<string, string> = {};
  if (kind === 'object_type')
    for (const [prop, col] of Object.entries(d.columnMap)) if (col) column_map[prop] = col;
  const base: Mapping = {
    target_kind: kind,
    target_key: key,
    connection_id: d.connectionId,
    relation: d.keyMode === 'link' && kind === 'relationship' ? d.linkRelation : d.relation,
    column_map,
    filters: d.filters.map((f) => f.trim()).filter(Boolean),
  };
  if (kind === 'relationship') {
    base.key_map =
      d.keyMode === 'link'
        ? { relation: d.linkRelation, from_column: d.fromColumn, to_column: d.toColumn }
        : { from_column: d.fromColumn, to_column: d.toColumn };
  }
  return base;
}

export function draftFromMapping(m: Mapping | undefined): MappingDraft {
  const rel = m?.relation ?? { schema: '', name: '' };
  const link = m?.key_map?.relation;
  return {
    connectionId: m?.connection_id ?? '',
    relation: link ? { schema: '', name: '' } : rel,
    columnMap: { ...(m?.column_map ?? {}) },
    filters: [...(m?.filters ?? [])],
    keyMode: link ? 'link' : 'direct',
    fromColumn: m?.key_map?.from_column ?? '',
    toColumn: m?.key_map?.to_column ?? '',
    linkRelation: link ?? { schema: '', name: '' },
  };
}

// ---- Expressions -----------------------------------------------------------------------------

const POLICY_ROOT = /^(row\.[A-Za-z_][A-Za-z0-9_]*|principal\.attributes\.[A-Za-z_][A-Za-z0-9_]*)$/;
const WORDS = new Set(['and', 'or', 'not', 'true', 'false', 'null']);

/**
 * Row filter syntax check (contract section 3): the safe expression language, where every field
 * reference must be `row.<property>` or `principal.attributes.<name>`. Calls, indexing and other
 * roots are rejected client-side; the server compiles it into parameters and stays authoritative.
 */
export function checkPolicyFilter(src: string): ExprResult {
  const base = checkExpression(src);
  if (!base.ok) return base;
  const withoutStrings = src.replace(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '""');
  for (const m of withoutStrings.matchAll(/[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*/g)) {
    if (WORDS.has(m[0])) continue;
    if (!POLICY_ROOT.test(m[0]))
      return {
        ok: false,
        pos: m.index,
        error: `Field "${m[0]}" is not allowed; use row.<property> or principal.attributes.<name>.`,
      };
  }
  return { ok: true };
}

/** Explorer and mapping filters: syntax only (fields are property keys or columns, bare or `row.`). */
export const checkFilter = (src: string): ExprResult => checkExpression(src);

// ---- Actions ----------------------------------------------------------------------------------

export const OBJECT_REF = '$object.';

/** The property a mapping value refers to (`$object.<prop>`), or null for a literal. */
export const mappedProperty = (value: string): string | null =>
  value.startsWith(OBJECT_REF) ? value.slice(OBJECT_REF.length) : null;

/** Properties referenced by `$object.<prop>` values that the object type does not define. */
export function missingMappedProperties(
  mapping: Record<string, string>,
  ot: Pick<ObjectType, 'properties'>,
): string[] {
  const known = new Set(ot.properties.map((p) => p.key));
  const out: string[] = [];
  for (const v of Object.values(mapping)) {
    const prop = mappedProperty(v);
    if (prop !== null && !known.has(prop) && !out.includes(prop)) out.push(prop);
  }
  return out;
}

export interface ToolParams {
  names: string[];
  required: string[];
}

/** Parameter names (and required ones) of a tool version's JSON input schema; null when unusable. */
export function toolParams(schema: unknown): ToolParams | null {
  if (!isRec(schema) || !isRec(schema.properties)) return null;
  const names = Object.keys(schema.properties);
  const required = Array.isArray(schema.required)
    ? schema.required.filter((r): r is string => typeof r === 'string')
    : [];
  return { names, required };
}

export interface ActionCheck {
  errors: string[];
  warnings: string[];
}

/**
 * Validates one action against the spec (and the tool input schema params when known). Errors block saving the
 * action; warnings (unknown params) are advisory because the server validates the tool call.
 */
export function checkAction(
  spec: OntologySpec,
  a: OntAction,
  opts: { editing: boolean; params?: ToolParams | null },
): ActionCheck {
  const errors: string[] = [];
  const warnings: string[] = [];
  if (!KEY_RE.test(a.key)) errors.push('Key: use lowercase letters, digits, ".", "-" or "_".');
  else if (!opts.editing && spec.actions.some((x) => x.key === a.key))
    errors.push('An action with this key already exists.');
  const ot = spec.object_types.find((o) => o.key === a.object_type);
  if (!ot) errors.push('Choose an object type that exists in this ontology.');
  if (!a.tool_version_id) errors.push('Choose a published tool version.');
  const names = Object.keys(a.param_mapping);
  if (names.some((n) => !n.trim())) errors.push('Every parameter needs a name.');
  if (names.some((n) => a.param_mapping[n] === '')) errors.push('Every parameter needs a value.');
  if (ot)
    for (const prop of missingMappedProperties(a.param_mapping, ot))
      errors.push(`Mapping refers to "${prop}", which is not a property of ${ot.key}.`);
  if (opts.params) {
    for (const r of opts.params.required)
      if (!names.includes(r)) errors.push(`Required tool parameter "${r}" is not mapped.`);
    for (const n of names)
      if (n && !opts.params.names.includes(n))
        warnings.push(`"${n}" is not a parameter of the selected tool version.`);
  }
  return { errors, warnings };
}

// ---- Query & object helpers ------------------------------------------------------------------

export interface HopOption {
  relationship: Relationship;
  /** Object type key reached by following it from `from`. */
  target: string;
  reverse: boolean;
}

/** Relationships traversable from an object type (outgoing, or incoming marked as reverse). */
export function hopOptions(spec: OntologySpec, from: string): HopOption[] {
  const out: HopOption[] = [];
  for (const r of spec.relationships) {
    if (r.from === from) out.push({ relationship: r, target: r.to, reverse: false });
    else if (r.to === from) out.push({ relationship: r, target: r.from, reverse: true });
  }
  return out;
}

/** The object type reached after following `hops` from `root` (null if a hop is not traversable). */
export function typeAtHop(
  spec: OntologySpec,
  root: string,
  hops: readonly { relationship: string }[],
): string | null {
  let cur: string | null = root;
  for (const h of hops) {
    if (!cur) return null;
    const opt: HopOption | undefined = hopOptions(spec, cur).find(
      (o) => o.relationship.key === h.relationship,
    );
    cur = opt ? opt.target : null;
  }
  return cur;
}

/** Primary key values of a row as the single `{pk}` path segment (composite keys joined by a comma). */
export function pkOfRow(ot: ObjectType, row: OntRow): string | null {
  if (ot.primary_key.length === 0) return null;
  const parts: string[] = [];
  for (const k of ot.primary_key) {
    const v = row[k];
    if (v === undefined || v === null) return null;
    parts.push(String(v));
  }
  return parts.join(',');
}

export function formatCell(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

const isRec = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

/**
 * Object view body: `{object|properties|..., relationships}`; relationship summaries may be a map
 * `{key: count}` or a list of `{relationship|key, count, capped?}` (contract leaves the shape open).
 */
export function normalizeObjectView(raw: unknown): ObjectView {
  if (!isRec(raw)) return { properties: {}, relationships: [] };
  const props = isRec(raw.object)
    ? raw.object
    : isRec(raw.properties)
      ? raw.properties
      : (() => {
          const { relationships: _r, ...rest } = raw;
          void _r;
          return rest;
        })();
  const rels: RelationshipSummary[] = [];
  const src = raw.relationships ?? raw.relationship_summaries;
  if (Array.isArray(src)) {
    for (const r of src) {
      if (!isRec(r)) continue;
      const key = r.relationship ?? r.key;
      if (typeof key === 'string')
        rels.push({
          relationship: key,
          count: typeof r.count === 'number' ? r.count : 0,
          ...(r.capped === true ? { capped: true } : {}),
          ...(typeof r.label === 'string' ? { label: r.label } : {}),
          ...(r.direction === 'forward' || r.direction === 'reverse'
            ? { direction: r.direction }
            : {}),
        });
    }
  } else if (isRec(src)) {
    for (const [k, v] of Object.entries(src)) {
      if (typeof v === 'number') rels.push({ relationship: k, count: v });
      else if (isRec(v))
        rels.push({
          relationship: k,
          count: typeof v.count === 'number' ? v.count : 0,
          ...(v.capped === true ? { capped: true } : {}),
        });
    }
  }
  return { properties: props, relationships: rels };
}

/** Related rows for a hop: `row._links[relationship]` (contract F1), falling back to `row[relationship]`; arrays of objects only. */
export function relatedRows(row: OntRow, relationship: string): OntRow[] {
  const direct = row[relationship];
  const links = isRec(row._links) ? row._links[relationship] : undefined;
  const found = Array.isArray(links) ? links : Array.isArray(direct) ? direct : [];
  return found.filter(isRec);
}
