import type {
  EdgeWhen,
  OrchEdge,
  OrchLimits,
  OrchNode,
  OrchNodeType,
  OrchestrationSpec,
} from './api/operate-types';
import { checkExpression } from './expr';

export const NODE_TYPES: readonly OrchNodeType[] = [
  'agent',
  'tool',
  'condition',
  'approval',
  'parallel',
];
export const EDGE_WHENS: readonly EdgeWhen[] = ['always', 'true', 'false'];

export function defaultLimits(): OrchLimits {
  return {
    max_nodes_executed: 20,
    max_depth: 3,
    max_total_tokens: 20000,
    deadline_s: 300,
    max_parallel: 4,
  };
}

export function emptyOrchestrationSpec(): OrchestrationSpec {
  return { nodes: [], edges: [], limits: defaultLimits() };
}

export function normalizeOrchestrationSpec(
  raw: Record<string, unknown> | undefined,
): OrchestrationSpec {
  const d = emptyOrchestrationSpec();
  if (!raw) return d;
  const r = raw as Partial<OrchestrationSpec>;
  return {
    nodes: (r.nodes ?? []).map((n) => ({ ...n, input_map: n.input_map ?? {} })),
    edges: r.edges ?? [],
    limits: { ...d.limits, ...(r.limits ?? {}) },
  };
}

/** Unique id like `agent_2` for a new node. */
export function nextNodeId(nodes: readonly OrchNode[], type: OrchNodeType): string {
  const used = new Set(nodes.map((n) => n.id));
  let i = 1;
  while (used.has(`${type}_${i}`)) i++;
  return `${type}_${i}`;
}

/**
 * Returns the node ids of one cycle (in order, first id repeated at the end) or null for a DAG.
 * Edges to unknown nodes are ignored here; they are reported separately.
 */
export function findCycle(
  nodes: readonly { id: string }[],
  edges: readonly { from: string; to: string }[],
): string[] | null {
  const ids = new Set(nodes.map((n) => n.id));
  const next = new Map<string, string[]>();
  for (const e of edges) {
    if (!ids.has(e.from) || !ids.has(e.to)) continue;
    next.set(e.from, [...(next.get(e.from) ?? []), e.to]);
  }
  const state = new Map<string, 1 | 2>(); // 1 = on the DFS stack, 2 = done
  const stack: string[] = [];
  const visit = (id: string): string[] | null => {
    state.set(id, 1);
    stack.push(id);
    for (const n of next.get(id) ?? []) {
      if (state.get(n) === 1) return [...stack.slice(stack.indexOf(n)), n];
      if (!state.has(n)) {
        const c = visit(n);
        if (c) return c;
      }
    }
    stack.pop();
    state.set(id, 2);
    return null;
  };
  for (const n of nodes) {
    if (!state.has(n.id)) {
      const c = visit(n.id);
      if (c) return c;
    }
  }
  return null;
}

export interface GraphIssue {
  /** Node id the issue belongs to, when it is node-specific. */
  nodeId?: string;
  message: string;
}

/** Client-side mirrors of the server's validation (DAG, refs present, condition syntax, caps). */
export function graphIssues(spec: OrchestrationSpec): GraphIssue[] {
  const out: GraphIssue[] = [];
  const ids = new Set<string>();
  for (const n of spec.nodes) {
    if (ids.has(n.id)) out.push({ nodeId: n.id, message: `Duplicate node id "${n.id}".` });
    ids.add(n.id);
  }
  if (spec.nodes.length === 0) out.push({ message: 'Add at least one node.' });
  for (const n of spec.nodes) {
    if (n.type === 'agent' && !n.ref?.agent_version_id)
      out.push({ nodeId: n.id, message: `Node "${n.id}": choose a published agent version.` });
    if (n.type === 'tool' && !n.ref?.tool_version_id)
      out.push({ nodeId: n.id, message: `Node "${n.id}": choose a published tool version.` });
    if (n.type === 'condition') {
      const r = checkExpression(n.condition ?? '');
      if (!r.ok)
        out.push({ nodeId: n.id, message: `Node "${n.id}" condition: ${r.error ?? 'invalid'}` });
    }
    if (
      n.type === 'parallel' &&
      n.max_parallel !== undefined &&
      n.max_parallel > spec.limits.max_parallel
    )
      out.push({
        nodeId: n.id,
        message: `Node "${n.id}": max parallel ${n.max_parallel} exceeds the limit ${spec.limits.max_parallel}.`,
      });
  }
  for (const e of spec.edges) {
    if (!ids.has(e.from) || !ids.has(e.to))
      out.push({ message: `Edge ${e.from} -> ${e.to} references a missing node.` });
    else if (e.from === e.to)
      out.push({ nodeId: e.from, message: `Node "${e.from}" links to itself.` });
    const src = spec.nodes.find((n) => n.id === e.from);
    if (src && src.type !== 'condition' && (e.when === 'true' || e.when === 'false'))
      out.push({
        nodeId: e.from,
        message: `Edge ${e.from} -> ${e.to}: only condition nodes can branch on true/false.`,
      });
  }
  const cycle = findCycle(spec.nodes, spec.edges);
  if (cycle)
    out.push({ message: `Cycle detected: ${cycle.join(' -> ')}. Orchestrations must be acyclic.` });
  return out;
}

/** Layered auto-layout (positions are not persisted; see README Decisions). */
export function autoLayout(
  nodes: readonly { id: string }[],
  edges: readonly { from: string; to: string }[],
): Map<string, { x: number; y: number }> {
  const layer = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  // Bounded relaxation: terminates even when the graph has a cycle.
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false;
    for (const e of edges) {
      const f = layer.get(e.from);
      const t = layer.get(e.to);
      if (f === undefined || t === undefined) continue;
      if (t < f + 1 && f + 1 < nodes.length) {
        layer.set(e.to, f + 1);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const rowsPerLayer = new Map<number, number>();
  const out = new Map<string, { x: number; y: number }>();
  for (const n of nodes) {
    const l = layer.get(n.id) ?? 0;
    const row = rowsPerLayer.get(l) ?? 0;
    rowsPerLayer.set(l, row + 1);
    out.set(n.id, { x: l * 240, y: row * 110 });
  }
  return out;
}

/** Removing a node also removes its edges and any parallel `branches` entries pointing at it. */
export function removeNode(spec: OrchestrationSpec, id: string): OrchestrationSpec {
  return {
    ...spec,
    nodes: spec.nodes
      .filter((n) => n.id !== id)
      .map((n) =>
        n.branches?.includes(id) ? { ...n, branches: n.branches.filter((b) => b !== id) } : n,
      ),
    edges: spec.edges.filter((e: OrchEdge) => e.from !== id && e.to !== id),
  };
}
