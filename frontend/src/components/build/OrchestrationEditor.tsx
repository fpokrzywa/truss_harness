'use client';

import dynamic from 'next/dynamic';
import { useEffect, useMemo, useRef, useState } from 'react';
import type {
  EdgeWhen,
  OrchEdge,
  OrchNode,
  OrchNodeType,
  OrchestrationSpec,
} from '@/lib/api/operate-types';
import { checkExpression } from '@/lib/expr';
import {
  EDGE_WHENS,
  graphIssues,
  NODE_TYPES,
  nextNodeId,
  normalizeOrchestrationSpec,
  removeNode,
} from '@/lib/graph';
import { SelectField, TextField } from '../admin/ui';
import type { EditorProps } from './DraftEditor';
import { VersionPicker } from './pickers';
import { Check, JsonField, NumberField, Section } from './shared';

export { normalizeOrchestrationSpec };

/** Save/publish blockers: client-side mirrors of server validation (DAG, refs, condition syntax). */
export const orchestrationBlockers = (spec: OrchestrationSpec): string[] =>
  graphIssues(spec).map((i) => i.message);

// React Flow measures the DOM, so it is loaded on the client only.
const OrchestrationCanvas = dynamic(
  () => import('./OrchestrationCanvas').then((m) => m.OrchestrationCanvas),
  { ssr: false, loading: () => <p role="status">Loading canvas...</p> },
);

type View = 'canvas' | 'list';

export function OrchestrationEditor({
  org,
  spec,
  onChange,
  readOnly,
  reportInvalid,
}: EditorProps<OrchestrationSpec>) {
  const [view, setView] = useState<View>('canvas');
  const [selected, setSelected] = useState<string | null>(null);
  // Canvas callbacks can fire several times per event (node + edge deletes); chain them on the latest spec.
  const latest = useRef(spec);
  useEffect(() => {
    latest.current = spec;
  }, [spec]);
  const update = (fn: (s: OrchestrationSpec) => OrchestrationSpec) => {
    latest.current = fn(latest.current);
    onChange(latest.current);
  };

  const issues = useMemo(() => graphIssues(spec), [spec]);
  const invalidIds = useMemo(
    () => new Set(issues.flatMap((i) => (i.nodeId ? [i.nodeId] : []))),
    [issues],
  );
  const node = spec.nodes.find((n) => n.id === selected) ?? null;

  function addNode(type: OrchNodeType) {
    const id = nextNodeId(latest.current.nodes, type);
    const fresh: OrchNode = {
      id,
      type,
      input_map: {},
      ...(type === 'condition' ? { condition: '' } : {}),
      ...(type === 'parallel' ? { branches: [] } : {}),
    };
    update((s) => ({ ...s, nodes: [...s.nodes, fresh] }));
    setSelected(id);
  }

  function addEdge(from: string, to: string, when: EdgeWhen = 'always') {
    update((s) =>
      s.edges.some((e) => e.from === from && e.to === to)
        ? s
        : { ...s, edges: [...s.edges, { from, to, when }] },
    );
  }

  function deleteNodes(ids: string[]) {
    update((s) => ids.reduce(removeNode, s));
    setSelected((cur) => (cur && ids.includes(cur) ? null : cur));
  }

  return (
    <div className="space-y-4">
      <Section
        title="Graph"
        help="Nodes run once their incoming edges resolve. Orchestrations must be acyclic. The server validates references, caps and expressions again on validate and publish."
      >
        <div className="flex flex-wrap items-center gap-2">
          {!readOnly &&
            NODE_TYPES.map((t) => (
              <button key={t} type="button" className="btn" onClick={() => addNode(t)}>
                Add {t} node
              </button>
            ))}
          <div role="group" aria-label="Graph view" className="ml-auto flex gap-1">
            <button
              type="button"
              className="btn"
              aria-pressed={view === 'canvas'}
              onClick={() => setView('canvas')}
            >
              Canvas view
            </button>
            <button
              type="button"
              className="btn"
              aria-pressed={view === 'list'}
              onClick={() => setView('list')}
            >
              List view
            </button>
          </div>
        </div>

        {view === 'canvas' ? (
          <OrchestrationCanvas
            spec={spec}
            selectedId={selected}
            invalidIds={invalidIds}
            readOnly={readOnly}
            onSelect={setSelected}
            onConnect={(f, t) => addEdge(f, t)}
            onDeleteNodes={deleteNodes}
            onDeleteEdges={(es) =>
              update((s) => ({
                ...s,
                edges: s.edges.filter((e) => !es.some((x) => x.from === e.from && x.to === e.to)),
              }))
            }
          />
        ) : (
          <NodeList spec={spec} selected={selected} onSelect={setSelected} />
        )}
        {spec.nodes.length === 0 && (
          <p className="text-sm text-slate-600">No nodes yet. Add one with the buttons above.</p>
        )}
      </Section>

      {node && (
        <NodePanel
          key={node.id}
          org={org}
          node={node}
          others={spec.nodes.filter((n) => n.id !== node.id)}
          limits={spec.limits}
          readOnly={readOnly}
          onChange={(next) =>
            update((s) => ({ ...s, nodes: s.nodes.map((n) => (n.id === next.id ? next : n)) }))
          }
          onRemove={() => deleteNodes([node.id])}
          reportInvalid={reportInvalid}
        />
      )}

      <EdgeList
        spec={spec}
        readOnly={readOnly}
        onAdd={(f, t) => addEdge(f, t)}
        onChange={(i, e) =>
          update((s) => ({ ...s, edges: s.edges.map((x, j) => (j === i ? e : x)) }))
        }
        onRemove={(i) => update((s) => ({ ...s, edges: s.edges.filter((_, j) => j !== i) }))}
      />

      <Section title="Limits" help="Execution caps enforced by the graph executor.">
        <div className="grid gap-3 sm:grid-cols-3">
          {(
            [
              ['max_nodes_executed', 'Max nodes executed'],
              ['max_depth', 'Max depth'],
              ['max_total_tokens', 'Max total tokens'],
              ['deadline_s', 'Deadline (seconds)'],
              ['max_parallel', 'Max parallel'],
            ] as const
          ).map(([k, label]) => (
            <NumberField
              key={k}
              label={label}
              min={1}
              fallback={spec.limits[k]}
              value={spec.limits[k]}
              disabled={readOnly}
              onChange={(v) => update((s) => ({ ...s, limits: { ...s.limits, [k]: v } }))}
            />
          ))}
        </div>
      </Section>
    </div>
  );
}

/** Keyboard-accessible alternative to the canvas: every node as a button. */
function NodeList({
  spec,
  selected,
  onSelect,
}: {
  spec: OrchestrationSpec;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  if (spec.nodes.length === 0) return null;
  return (
    <ul aria-label="Nodes" className="divide-y divide-slate-100 rounded-md border border-slate-200">
      {spec.nodes.map((n) => {
        const out = spec.edges.filter((e) => e.from === n.id);
        return (
          <li key={n.id} className="flex flex-wrap items-center gap-2 p-2 text-sm">
            <button
              type="button"
              className="btn"
              aria-pressed={selected === n.id}
              aria-label={`Select ${n.type} node ${n.id}`}
              onClick={() => onSelect(n.id)}
            >
              {n.id}
            </button>
            <span className="text-slate-600">{n.type}</span>
            {out.length > 0 && (
              <span className="text-xs text-slate-600">
                to{' '}
                {out
                  .map((e) => `${e.to}${e.when && e.when !== 'always' ? ` (${e.when})` : ''}`)
                  .join(', ')}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function NodePanel({
  org,
  node,
  others,
  limits,
  readOnly,
  onChange,
  onRemove,
  reportInvalid,
}: {
  org: string;
  node: OrchNode;
  others: OrchNode[];
  limits: OrchestrationSpec['limits'];
  readOnly: boolean;
  onChange: (n: OrchNode) => void;
  onRemove: () => void;
  reportInvalid: (field: string, message: string | null) => void;
}) {
  const expr = node.type === 'condition' ? checkExpression(node.condition ?? '') : null;
  return (
    <section aria-label={`Configure node ${node.id}`} className="card space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">
          Node {node.id} <span className="font-normal text-slate-600">({node.type})</span>
        </h3>
        {!readOnly && (
          <button type="button" className="btn btn-danger" onClick={onRemove}>
            Remove node {node.id}
          </button>
        )}
      </div>

      {node.type === 'agent' && (
        <VersionPicker
          org={org}
          kind="agents"
          label="Agent"
          value={node.ref?.agent_version_id ?? ''}
          disabled={readOnly}
          onChange={(v) => onChange({ ...node, ref: v ? { agent_version_id: v } : undefined })}
        />
      )}
      {node.type === 'tool' && (
        <VersionPicker
          org={org}
          kind="tools"
          label="Tool"
          value={node.ref?.tool_version_id ?? ''}
          disabled={readOnly}
          onChange={(v) => onChange({ ...node, ref: v ? { tool_version_id: v } : undefined })}
        />
      )}
      {node.type === 'condition' && (
        <TextField
          label="Condition expression"
          value={node.condition ?? ''}
          monospace
          disabled={readOnly}
          onChange={(v) => onChange({ ...node, condition: v })}
          help={
            'Literals, comparisons, and / or / not, dotted field access. Example: classify.output.score >= 0.8 and not review.output.flagged. No function calls.'
          }
          error={expr && !expr.ok ? expr.error : null}
        />
      )}
      {node.type === 'parallel' && (
        <>
          <NumberField
            label="Max parallel (this node)"
            min={1}
            fallback={limits.max_parallel}
            value={node.max_parallel ?? limits.max_parallel}
            disabled={readOnly}
            onChange={(v) => onChange({ ...node, max_parallel: v })}
          />
          <fieldset>
            <legend className="label">Branches</legend>
            {others.length === 0 && <p className="text-xs text-slate-600">No other nodes.</p>}
            {others.map((o) => (
              <Check
                key={o.id}
                label={`${o.id} (${o.type})`}
                checked={node.branches?.includes(o.id) ?? false}
                disabled={readOnly}
                onChange={(on) =>
                  onChange({
                    ...node,
                    branches: on
                      ? [...(node.branches ?? []), o.id]
                      : (node.branches ?? []).filter((b) => b !== o.id),
                  })
                }
              />
            ))}
          </fieldset>
        </>
      )}
      {node.type === 'approval' && (
        <p className="text-xs text-slate-600">
          Pauses the run until an approver decides in the Approvals queue.
        </p>
      )}

      <JsonField
        label={`Input map for ${node.id} (JSON object)`}
        value={node.input_map}
        readOnly={readOnly}
        rows={5}
        help="Maps this node's inputs to values or to prior node outputs."
        check={(v) =>
          typeof v === 'object' && v !== null && !Array.isArray(v) ? [] : ['Must be a JSON object.']
        }
        onInvalid={(m) => reportInvalid(`input_map:${node.id}`, m)}
        onChange={(v) => onChange({ ...node, input_map: (v ?? {}) as Record<string, unknown> })}
      />
    </section>
  );
}

function EdgeList({
  spec,
  readOnly,
  onAdd,
  onChange,
  onRemove,
}: {
  spec: OrchestrationSpec;
  readOnly: boolean;
  onAdd: (from: string, to: string) => void;
  onChange: (index: number, edge: OrchEdge) => void;
  onRemove: (index: number) => void;
}) {
  const ids = spec.nodes.map((n) => ({ value: n.id, label: n.id }));
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  return (
    <Section
      title="Edges"
      help="An edge runs its target after its source. Condition nodes branch with when = true or false; otherwise use always."
    >
      {spec.edges.length === 0 && <p className="text-sm text-slate-600">No edges.</p>}
      <ul className="space-y-2">
        {spec.edges.map((e, i) => (
          <li key={`${e.from}-${e.to}`} className="flex flex-wrap items-end gap-2">
            <span className="pb-2 text-sm">
              {e.from} {'->'} {e.to}
            </span>
            <SelectField
              label={`When for edge ${e.from} to ${e.to}`}
              value={e.when ?? 'always'}
              disabled={readOnly}
              onChange={(w) => onChange(i, { ...e, when: w as EdgeWhen })}
              options={EDGE_WHENS.map((w) => ({ value: w, label: w }))}
            />
            {!readOnly && (
              <button
                type="button"
                className="btn"
                aria-label={`Remove edge ${e.from} to ${e.to}`}
                onClick={() => onRemove(i)}
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>
      {!readOnly && spec.nodes.length >= 2 && (
        <div className="flex flex-wrap items-end gap-2 border-t border-slate-200 pt-3">
          <SelectField
            label="Edge from"
            value={from}
            onChange={setFrom}
            options={[{ value: '', label: 'Select...' }, ...ids]}
          />
          <SelectField
            label="Edge to"
            value={to}
            onChange={setTo}
            options={[{ value: '', label: 'Select...' }, ...ids]}
          />
          <button
            type="button"
            className="btn"
            disabled={!from || !to}
            onClick={() => {
              onAdd(from, to);
              setFrom('');
              setTo('');
            }}
          >
            Add edge
          </button>
        </div>
      )}
    </Section>
  );
}
