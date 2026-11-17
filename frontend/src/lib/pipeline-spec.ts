import type {
  EdgeWhenValue,
  NodeRunStatus,
  NodeTypeDef,
  PipelineEdge,
  PipelineNode,
  PipelinePolicies,
  PipelineSpec,
  Position,
} from './api/pipeline-types';
import { checkExpression, type ExprResult } from './expr';
import { autoLayout, findCycle } from './graph';

// ---- Spec shape ----------------------------------------------------------------------------

export function defaultPolicies(): PipelinePolicies {
  return { max_parallel: 4, deadline_seconds: 3600, on_failure: 'fail_fast' };
}

export function emptyPipelineSpec(): PipelineSpec {
  return {
    schema_version: '1',
    params: { type: 'object', properties: {} },
    policies: defaultPolicies(),
    nodes: [],
    edges: [],
    schemas: {},
  };
}

/** Fills defaults but keeps every unknown key (the server schema is authoritative). */
export function normalizePipelineSpec(raw: Record<string, unknown> | undefined): PipelineSpec {
  const d = emptyPipelineSpec();
  if (!raw) return d;
  const r = raw as Partial<PipelineSpec>;
  return {
    ...r,
    schema_version: r.schema_version ?? d.schema_version,
    params: r.params ?? d.params,
    policies: { ...d.policies, ...(r.policies ?? {}) },
    nodes: (r.nodes ?? []).map((n) => ({ ...n, config: n.config ?? {}, inputs: n.inputs ?? {} })),
    // Legacy boolean / null conditions are rewritten to the strings the server accepts.
    edges: (r.edges ?? []).map((e) =>
      typeof e.when === 'boolean'
        ? { ...e, when: String(e.when) }
        : e.when === null
          ? withEdgeWhen(e, 'always')
          : e,
    ),
    schemas: r.schemas ?? {},
  };
}

/**
 * Reads an edge condition. The server (amendment E1) stores `"true" | "false" | "always"`; missing or null
 * means always. Legacy booleans are tolerated. Anything else is `custom` (kept untouched, server rejects it).
 */
export function edgeWhen(e: Pick<PipelineEdge, 'when'>): EdgeWhenValue | 'custom' {
  const w = e.when;
  if (w === undefined || w === null || w === 'always') return 'always';
  if (w === true || w === 'true') return 'true';
  if (w === false || w === 'false') return 'false';
  return 'custom';
}

/** Canvas label: `always` (the default) has none; true / false are shown; anything else is a custom condition. */
export function edgeLabel(e: Pick<PipelineEdge, 'when'>): string | undefined {
  const w = edgeWhen(e);
  return w === 'always' ? undefined : w === 'custom' ? 'condition' : w;
}

/** Edge with its condition set to a server-valid value (`always` is stored as the absence of `when`). */
export function withEdgeWhen(e: PipelineEdge, when: EdgeWhenValue): PipelineEdge {
  const { when: _drop, ...rest } = e;
  void _drop;
  return when === 'always' ? rest : { ...rest, when };
}

export const edgeKey = (e: { from: string; to: string }) => `${e.from}->${e.to}`;

/** Unique id like `transform_2` for a new node. */
export function nextNodeId(nodes: readonly { id: string }[], type: string): string {
  const used = new Set(nodes.map((n) => n.id));
  let i = 1;
  while (used.has(`${type}_${i}`)) i++;
  return `${type}_${i}`;
}

export function newNode(spec: PipelineSpec, type: string): PipelineNode {
  return { id: nextNodeId(spec.nodes, type), type, config: {}, inputs: {} };
}

// ---- Graph helpers -------------------------------------------------------------------------

/** All nodes with a path to `id` (edges to unknown nodes are ignored). */
export function ancestorsOf(id: string, edges: readonly PipelineEdge[]): Set<string> {
  const incoming = new Map<string, string[]>();
  for (const e of edges) incoming.set(e.to, [...(incoming.get(e.to) ?? []), e.from]);
  const seen = new Set<string>();
  const stack = [...(incoming.get(id) ?? [])];
  while (stack.length) {
    const cur = stack.pop() as string;
    if (seen.has(cur)) continue;
    seen.add(cur);
    stack.push(...(incoming.get(cur) ?? []));
  }
  return seen;
}

/** True when adding from -> to would close a cycle (or link a node to itself). */
export function wouldCreateCycle(
  spec: Pick<PipelineSpec, 'nodes' | 'edges'>,
  from: string,
  to: string,
): boolean {
  return from === to || ancestorsOf(from, spec.edges).has(to);
}

export type EdgeResult = { spec: PipelineSpec; error?: string };

/** Adds an edge unless it is a duplicate, references a missing node, or would create a cycle. */
export function addEdgeChecked(
  spec: PipelineSpec,
  from: string,
  to: string,
  when?: unknown,
): EdgeResult {
  const ids = new Set(spec.nodes.map((n) => n.id));
  if (!ids.has(from) || !ids.has(to))
    return { spec, error: `Edge ${from} -> ${to} references a missing node.` };
  if (spec.edges.some((e) => e.from === from && e.to === to)) return { spec };
  if (wouldCreateCycle(spec, from, to))
    return {
      spec,
      error: `Edge ${from} -> ${to} would create a cycle; pipelines must be acyclic.`,
    };
  return {
    spec: {
      ...spec,
      edges: [...spec.edges, { from, to, ...(when === undefined ? {} : { when }) }],
    },
  };
}

/** Removing a node also removes its edges, its position and input bindings that point at it. */
export function removePipelineNode(spec: PipelineSpec, id: string): PipelineSpec {
  const positions = { ...(spec.ui?.positions ?? {}) };
  delete positions[id];
  return {
    ...spec,
    nodes: spec.nodes
      .filter((n) => n.id !== id)
      .map((n) => ({
        ...n,
        inputs: Object.fromEntries(
          Object.entries(n.inputs).filter(([, v]) => !v.startsWith(`${id}.`)),
        ),
      })),
    edges: spec.edges.filter((e) => e.from !== id && e.to !== id),
    ...(spec.ui ? { ui: { ...spec.ui, positions } } : {}),
  };
}

export function setPosition(spec: PipelineSpec, id: string, pos: Position): PipelineSpec {
  const rounded = { x: Math.round(pos.x), y: Math.round(pos.y) };
  return {
    ...spec,
    ui: { ...(spec.ui ?? {}), positions: { ...(spec.ui?.positions ?? {}), [id]: rounded } },
  };
}

/** Persisted positions win; unpositioned nodes fall back to the layered auto-layout. */
export function layoutPositions(spec: PipelineSpec): Map<string, Position> {
  const auto = autoLayout(spec.nodes, spec.edges);
  const out = new Map<string, Position>();
  for (const n of spec.nodes) {
    out.set(n.id, spec.ui?.positions?.[n.id] ?? auto.get(n.id) ?? { x: 0, y: 0 });
  }
  return out;
}

// ---- Ports and bindings --------------------------------------------------------------------

export interface BindingOption {
  value: string;
  label: string;
}

/** Outputs a node may bind an input to: ports of upstream nodes (by registry) and `$params.*`. */
export function bindingOptions(
  spec: PipelineSpec,
  registry: readonly NodeTypeDef[],
  nodeId: string,
): BindingOption[] {
  const up = ancestorsOf(nodeId, spec.edges);
  const out: BindingOption[] = [];
  for (const n of spec.nodes) {
    if (!up.has(n.id)) continue;
    const def = registry.find((t) => t.type === n.type);
    for (const o of def?.outputs ?? [])
      out.push({ value: `${n.id}.${o.name}`, label: `${n.id}.${o.name}` });
  }
  const props = (spec.params as { properties?: Record<string, unknown> }).properties ?? {};
  for (const k of Object.keys(props)) out.push({ value: `$params.${k}`, label: `$params.${k}` });
  return out;
}

/** Writes (or clears, for an empty value) `inputs[name] = "node.output"`. */
export function bindInput(node: PipelineNode, name: string, value: string): PipelineNode {
  const inputs = { ...node.inputs };
  if (value) inputs[name] = value;
  else delete inputs[name];
  return { ...node, inputs };
}

// ---- Client-side validation (mirrors server rules; the server is authoritative) -------------

export interface PipelineIssue {
  code: string;
  message: string;
  nodeId?: string;
  edge?: { from: string; to: string };
  severity: 'error' | 'warning';
  /** Blocking issues disable save/publish; the rest are advisory (registry-dependent or warnings). */
  blocking: boolean;
}

const IDENT = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
/** Port names usable from a safe expression (`inputs.<port>`) and as `{placeholder}` names. */
export const PORT_NAME = /^[A-Za-z][A-Za-z0-9_]{0,62}$/;

/** Safe-expression check plus the server's root rule: only `allowed` names may be read. */
export function checkPipelineExpression(src: string, allowed: readonly string[]): ExprResult {
  const r = checkExpression(src);
  if (!r.ok) return r;
  const bad = (r.paths ?? []).some((p) => !allowed.includes(String(p[0])));
  return bad
    ? { ok: false, error: `An expression can only read ${[...allowed].sort().join(', ')}.`, pos: 0 }
    : r;
}

const MODEL_OUTPUT_TYPES = new Set(['model', 'prompt', 'agent']);

/** `config.operations` (amendment E1; the draft UI used to write `operators`). */
export const OPERATIONS_KEY = 'operations';

function operatorExpressions(node: PipelineNode): { where: string; expr: string }[] {
  if (node.type !== 'transform' || !Array.isArray(node.config[OPERATIONS_KEY])) return [];
  const out: { where: string; expr: string }[] = [];
  (node.config[OPERATIONS_KEY] as Record<string, unknown>[]).forEach((op, i) => {
    if (
      (op.op === 'filter' || op.op === 'map') &&
      typeof op.expression === 'string' &&
      op.expression
    )
      out.push({ where: `operator ${i + 1} (${String(op.op)})`, expr: op.expression });
  });
  return out;
}

/** Whether a node performs an external write (connection sink, non-GET request, side-effect registry types). */
export function isSideEffectNode(node: PipelineNode, registry: readonly NodeTypeDef[]): boolean {
  if (node.type === 'sink') return node.config.target === 'connection';
  if (node.type === 'api_request') {
    const m = typeof node.config.method === 'string' ? node.config.method.toUpperCase() : 'GET';
    return m !== 'GET';
  }
  return registry.find((t) => t.type === node.type)?.side_effect === true;
}

export function pipelineIssues(
  spec: PipelineSpec,
  registry?: readonly NodeTypeDef[],
): PipelineIssue[] {
  const out: PipelineIssue[] = [];
  const err = (i: Omit<PipelineIssue, 'severity' | 'blocking'> & { blocking?: boolean }) =>
    out.push({ severity: 'error', blocking: true, ...i });
  const ids = new Set<string>();
  for (const n of spec.nodes) {
    if (ids.has(n.id))
      err({ code: 'duplicate_node_id', nodeId: n.id, message: `Duplicate node id "${n.id}".` });
    ids.add(n.id);
  }
  if (registry && registry.length > 0) {
    for (const n of spec.nodes) {
      const def = registry.find((t) => t.type === n.type);
      if (!def)
        err({
          code: 'unknown_node_type',
          nodeId: n.id,
          blocking: false,
          message: `Node "${n.id}" has unknown type "${n.type}".`,
        });
      else if (!def.enabled)
        err({
          code: 'node_type_disabled',
          nodeId: n.id,
          blocking: false,
          message: `Node "${n.id}": type "${n.type}" is not available${def.reason_disabled ? ` (${def.reason_disabled})` : ''}.`,
        });
    }
  }
  const validEdges: PipelineEdge[] = [];
  for (const e of spec.edges) {
    if (!ids.has(e.from) || !ids.has(e.to))
      err({
        code: 'dangling_edge',
        edge: { from: e.from, to: e.to },
        message: `Edge ${e.from} -> ${e.to} references a missing node.`,
      });
    else validEdges.push(e);
  }
  const cycle = findCycle(spec.nodes, spec.edges);
  if (cycle)
    err({
      code: 'cycle_detected',
      nodeId: cycle[0],
      message: `Cycle detected: ${cycle.join(' -> ')}. Pipelines must be acyclic.`,
    });

  for (const n of spec.nodes) {
    const up = ancestorsOf(n.id, validEdges);
    for (const [port, ref] of Object.entries(n.inputs)) {
      if (ref.startsWith('$params.')) continue;
      const src = ref.split('.')[0] ?? '';
      if (!up.has(src))
        err({
          code: 'input_not_upstream',
          nodeId: n.id,
          message: `Node "${n.id}" input "${port}" is bound to "${ref}", which is not an upstream output.`,
        });
    }
    if (n.type === 'condition' && typeof n.config.expression === 'string') {
      const r = checkPipelineExpression(n.config.expression, ['inputs', 'params']);
      if (!r.ok)
        err({
          code: 'expression_invalid',
          nodeId: n.id,
          message: `Node "${n.id}" condition: ${r.error ?? 'invalid expression'}`,
        });
      else {
        for (const p of r.paths ?? []) {
          if (p[0] === 'inputs' && (typeof p[1] !== 'string' || !Object.hasOwn(n.inputs, p[1])))
            err({
              code: 'expression_invalid',
              nodeId: n.id,
              message: `Node "${n.id}" condition: inputs.<port> must name a bound input port.`,
            });
        }
      }
    }
    for (const { where, expr } of operatorExpressions(n)) {
      const r = checkPipelineExpression(expr, ['row', 'params']);
      if (!r.ok)
        err({
          code: 'expression_invalid',
          nodeId: n.id,
          message: `Node "${n.id}" ${where}: ${r.error ?? 'invalid expression'}`,
        });
    }
    // Input ports must be declared by the type, unless the type names its inputs itself (dynamic_inputs).
    const def = registry?.find((t) => t.type === n.type);
    if (def && !def.dynamic_inputs) {
      for (const port of Object.keys(n.inputs))
        if (!def.inputs.some((p) => p.name === port))
          err({
            code: 'unknown_port',
            nodeId: n.id,
            blocking: false,
            message: `Node "${n.id}": ${n.type} has no input port "${port}".`,
          });
    }
  }

  // Edge conditions: only true / false / always; true / false only leave a condition node.
  const typeById = new Map(spec.nodes.map((n) => [n.id, n.type]));
  for (const e of validEdges) {
    const w = edgeWhen(e);
    if (w === 'custom')
      err({
        code: 'expression_invalid',
        edge: { from: e.from, to: e.to },
        message: `Edge ${e.from} -> ${e.to}: when must be true, false or always.`,
      });
    else if (w !== 'always' && typeById.get(e.from) !== 'condition')
      err({
        code: 'invalid_edge_condition',
        edge: { from: e.from, to: e.to },
        message: `Edge ${e.from} -> ${e.to}: only edges leaving a condition node can be true or false.`,
      });
  }

  // Rule 8: side effects downstream of model output need an approval node on the path.
  if (!cycle) {
    const next = new Map<string, string[]>();
    for (const e of validEdges) next.set(e.from, [...(next.get(e.from) ?? []), e.to]);
    const unguarded = new Set<string>();
    for (const m of spec.nodes.filter((n) => MODEL_OUTPUT_TYPES.has(n.type))) {
      const stack = [...(next.get(m.id) ?? [])];
      while (stack.length) {
        const cur = stack.pop() as string;
        if (unguarded.has(cur)) continue;
        unguarded.add(cur);
        const node = spec.nodes.find((n) => n.id === cur);
        if (node?.type === 'approval') continue; // an approval on the path guards everything past it
        stack.push(...(next.get(cur) ?? []));
      }
    }
    for (const n of spec.nodes) {
      if (unguarded.has(n.id) && n.type !== 'approval' && isSideEffectNode(n, registry ?? []))
        out.push({
          code: 'approval_required_before_side_effect',
          nodeId: n.id,
          severity: 'warning',
          blocking: false,
          message: `Node "${n.id}" has an external side effect after model output without an approval node on the path.`,
        });
    }
  }
  return out;
}

/** Messages that disable save/publish (structural, independent of the registry). */
export const pipelineBlockers = (spec: PipelineSpec): string[] =>
  pipelineIssues(spec)
    .filter((i) => i.blocking)
    .map((i) => i.message);

// ---- Node status overlay -------------------------------------------------------------------

export const NODE_STATUS_LABEL: Record<NodeRunStatus, string> = {
  pending: 'Pending',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  skipped: 'Skipped',
  cancelled: 'Cancelled',
  waiting_for_approval: 'Waiting for approval',
  outcome_unknown: 'Write outcome unknown',
};

/** Tailwind classes for the canvas node border/background; the text label is the accessible signal. */
export const NODE_STATUS_CLASS: Record<NodeRunStatus, string> = {
  pending: '!border-slate-400 !bg-white',
  running: '!border-blue-600 !bg-blue-50',
  succeeded: '!border-green-700 !bg-green-50',
  failed: '!border-red-700 !bg-red-50',
  skipped: '!border-slate-300 !bg-slate-100 opacity-70',
  cancelled: '!border-slate-500 !bg-slate-100',
  waiting_for_approval: '!border-amber-600 !bg-amber-50',
  outcome_unknown: '!border-amber-700 !bg-amber-50',
};

/** Display status of a node: a node with `outcome_unknown` reads as unknown whatever status it carries (R1.4). */
export function nodeDisplayStatus(node: {
  status: NodeRunStatus;
  outcome_unknown?: boolean;
}): NodeRunStatus {
  // The server flags the node itself; the status and error class alone do not say it (R1.4).
  return node.outcome_unknown ? 'outcome_unknown' : node.status;
}

export const nodeStatusLabel = (s: string): string => NODE_STATUS_LABEL[s as NodeRunStatus] ?? s;

// ---- Transform operators (structured rows, no free code) -------------------------------------

export type OperatorKind = 'select' | 'rename' | 'filter' | 'map' | 'cast' | 'aggregate';
export const OPERATOR_KINDS: readonly OperatorKind[] = [
  'select',
  'rename',
  'filter',
  'map',
  'cast',
  'aggregate',
];
export const CAST_TYPES = ['string', 'number', 'integer', 'boolean'] as const;
export const AGG_FUNCTIONS = ['count', 'sum', 'avg', 'min', 'max'] as const;

export type Operator =
  | { op: 'select'; fields: string[] }
  | { op: 'rename'; from: string; to: string }
  | { op: 'filter'; expression: string }
  | { op: 'map'; field: string; expression: string }
  | { op: 'cast'; field: string; to: (typeof CAST_TYPES)[number] }
  | {
      op: 'aggregate';
      group_by: string[];
      fn: (typeof AGG_FUNCTIONS)[number];
      field: string;
      as: string;
    };

export function newOperator(kind: OperatorKind): Operator {
  switch (kind) {
    case 'select':
      return { op: 'select', fields: [] };
    case 'rename':
      return { op: 'rename', from: '', to: '' };
    case 'filter':
      return { op: 'filter', expression: '' };
    case 'map':
      return { op: 'map', field: '', expression: '' };
    case 'cast':
      return { op: 'cast', field: '', to: 'string' };
    case 'aggregate':
      return { op: 'aggregate', group_by: [], fn: 'count', field: '', as: '' };
  }
}

const FIELD_NAME_HINT =
  'Field names are letters, digits and underscores (up to 64), not starting with a digit.';

/**
 * Rows as stored in `config.operations`. An empty aggregate `field` is omitted: the server counts only
 * non-null values of a named field, so `count` with `field: ""` would always yield 0.
 */
export function serializeOperators(ops: readonly Operator[]): Record<string, unknown>[] {
  return ops.map((op) => {
    if (op.op === 'aggregate' && !op.field) {
      const { field: _drop, ...rest } = op;
      void _drop;
      return rest;
    }
    return { ...op };
  });
}

export function operatorProblem(op: Operator): string | null {
  switch (op.op) {
    case 'select':
      return op.fields.length === 0
        ? 'Select needs at least one field.'
        : op.fields.some((f) => !IDENT.test(f))
          ? FIELD_NAME_HINT
          : null;
    case 'rename':
      return !op.from || !op.to
        ? 'Rename needs a source and a target name.'
        : !IDENT.test(op.from) || !IDENT.test(op.to)
          ? FIELD_NAME_HINT
          : null;
    case 'filter':
    case 'map': {
      if (op.op === 'map' && !op.field) return 'Map needs a target field.';
      if (op.op === 'map' && !IDENT.test(op.field)) return FIELD_NAME_HINT;
      if (!op.expression.trim())
        return `${op.op === 'map' ? 'Map' : 'Filter'} needs an expression.`;
      const r = checkPipelineExpression(op.expression, ['row', 'params']);
      return r.ok ? null : (r.error ?? 'Invalid expression.');
    }
    case 'cast':
      return !op.field ? 'Cast needs a field.' : !IDENT.test(op.field) ? FIELD_NAME_HINT : null;
    case 'aggregate':
      return !op.as
        ? 'Aggregate needs an output name.'
        : !IDENT.test(op.as) || op.group_by.some((g) => !IDENT.test(g))
          ? FIELD_NAME_HINT
          : op.fn !== 'count' && !op.field
            ? 'Aggregate needs a field.'
            : op.fn !== 'count' && !IDENT.test(op.field)
              ? FIELD_NAME_HINT
              : null;
  }
}

/** Parses stored `config.operations`, dropping entries that are not recognizable rows. */
export function parseOperators(raw: unknown): Operator[] {
  if (!Array.isArray(raw)) return [];
  const out: Operator[] = [];
  for (const r of raw as Record<string, unknown>[]) {
    const kind = r?.op as OperatorKind;
    if (!OPERATOR_KINDS.includes(kind)) continue;
    const base = newOperator(kind) as Record<string, unknown>;
    out.push({ ...base, ...r } as unknown as Operator);
  }
  return out;
}
