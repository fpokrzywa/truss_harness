'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type {
  EdgeWhenValue,
  NodeRunStatus,
  OnFailure,
  PipelineEdge,
  PipelineSpec,
} from '@/lib/api/pipeline-types';
import { useNodeTypes, useRunOverlay } from '@/lib/pipeline-hooks';
import {
  addEdgeChecked,
  edgeKey,
  edgeWhen,
  newNode,
  nodeStatusLabel,
  pipelineBlockers,
  pipelineIssues,
  removePipelineNode,
  setPosition,
  withEdgeWhen,
} from '@/lib/pipeline-spec';
import { schemaErrors } from '@/lib/tool-rules';
import { SelectField } from '../admin/ui';
import type { EditorProps } from '../build/DraftEditor';
import { JsonField, NumberField, Section } from '../build/shared';
import { LazyPipelineCanvas as PipelineCanvas } from './LazyPipelineCanvas';
import { NodeConfigPanel } from './NodeConfigPanel';
import { NodePalette } from './NodePalette';
import { RunStatusChip } from '../operate/parts';
import { usePipelineRun } from './PipelineRunContext';

export { normalizePipelineSpec } from '@/lib/pipeline-spec';
export { pipelineBlockers };

type View = 'canvas' | 'list';

const whenValue = (e: PipelineEdge): string => edgeWhen(e);

export function PipelineEditor({
  org,
  spec,
  onChange,
  readOnly,
  reportInvalid,
  validation,
}: EditorProps<PipelineSpec>) {
  const [view, setView] = useState<View>('canvas');
  const [selected, setSelected] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const types = useNodeTypes();
  const registry = useMemo(() => types.data ?? [], [types.data]);

  // Canvas callbacks can fire several times per event; chain them on the latest spec.
  const latest = useRef(spec);
  useEffect(() => {
    latest.current = spec;
  }, [spec]);
  const update = (fn: (s: PipelineSpec) => PipelineSpec) => {
    latest.current = fn(latest.current);
    onChange(latest.current);
  };

  // Live run overlay.
  const { runId } = usePipelineRun();
  const { run, statuses } = useRunOverlay(org, runId);

  // Highlights: client checks plus the latest server validation.
  const issues = useMemo(() => pipelineIssues(spec, registry), [spec, registry]);
  const invalidNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const i of issues) if (i.severity === 'error' && i.nodeId) ids.add(i.nodeId);
    for (const e of validation?.errors ?? []) if (e.node_id) ids.add(e.node_id);
    return ids;
  }, [issues, validation]);
  const invalidEdgeKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const i of issues) if (i.edge) keys.add(edgeKey(i.edge));
    for (const e of validation?.errors ?? []) if (e.edge) keys.add(edgeKey(e.edge));
    return keys;
  }, [issues, validation]);
  const advisory = issues.filter((i) => !i.blocking);

  const node = spec.nodes.find((n) => n.id === selected) ?? null;

  function addNodeOfType(type: string, pos?: { x: number; y: number }) {
    const fresh = newNode(latest.current, type);
    update((s) => {
      const next = { ...s, nodes: [...s.nodes, fresh] };
      return pos ? setPosition(next, fresh.id, pos) : next;
    });
    setSelected(fresh.id);
  }

  function addEdge(from: string, to: string, when?: unknown) {
    const r = addEdgeChecked(latest.current, from, to, when);
    setNotice(r.error ?? null);
    if (!r.error) update(() => r.spec);
  }

  function deleteNodes(ids: string[]) {
    update((s) => ids.reduce(removePipelineNode, s));
    setSelected((cur) => (cur && ids.includes(cur) ? null : cur));
  }

  return (
    <div className="space-y-4">
      <Section
        title="Pipeline graph"
        help="Add nodes from the palette, connect them, then bind each input to an upstream output. The server validates types, references and approvals again on validate and publish."
      >
        <NodePalette
          types={types.data}
          loading={types.isPending}
          error={types.error}
          readOnly={readOnly}
          onAdd={(t) => addNodeOfType(t)}
        />
        <div role="group" aria-label="Graph view" className="flex justify-end gap-1">
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
        {notice && (
          <p role="alert" className="text-sm text-red-700">
            {notice}
          </p>
        )}
        {view === 'canvas' ? (
          <PipelineCanvas
            spec={spec}
            registry={registry}
            selectedId={selected}
            invalidNodeIds={invalidNodeIds}
            invalidEdgeKeys={invalidEdgeKeys}
            statuses={statuses}
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
            onMoveNode={(id, pos) => update((s) => setPosition(s, id, pos))}
            onDropType={addNodeOfType}
          />
        ) : (
          <NodeList
            spec={spec}
            selected={selected}
            statuses={statuses}
            invalid={invalidNodeIds}
            onSelect={setSelected}
          />
        )}
        {spec.nodes.length === 0 && (
          <p className="text-sm text-slate-600">No nodes yet. Add one from the palette above.</p>
        )}
        {runId && (
          <p role="status" className="text-xs text-slate-600">
            Showing live node status for run {runId}
            {run && (
              <>
                {' '}
                <RunStatusChip status={run.status} outcomeUnknown={run.outcome_unknown} />
              </>
            )}
          </p>
        )}
      </Section>

      {node && (
        <NodeConfigPanel
          key={node.id}
          org={org}
          node={node}
          def={registry.find((t) => t.type === node.type)}
          spec={spec}
          registry={registry}
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

      <Section
        title="Parameters"
        help="JSON Schema (object) for the parameters a run accepts. Nodes can bind inputs to $params.name; the Run and Test panels build a form from it."
      >
        <JsonField
          label="Parameters schema (JSON)"
          value={spec.params}
          readOnly={readOnly}
          rows={8}
          check={(v) => schemaErrors(v)}
          onInvalid={(m) => reportInvalid('params', m)}
          onChange={(v) =>
            update((s) => ({
              ...s,
              params: (v ?? { type: 'object', properties: {} }) as Record<string, unknown>,
            }))
          }
        />
      </Section>

      <Section title="Policies" help="Execution policies enforced by the graph executor.">
        <div className="grid gap-3 sm:grid-cols-3">
          <NumberField
            label="Max parallel"
            min={1}
            fallback={spec.policies.max_parallel}
            value={spec.policies.max_parallel}
            disabled={readOnly}
            onChange={(v) =>
              update((s) => ({ ...s, policies: { ...s.policies, max_parallel: v } }))
            }
          />
          <NumberField
            label="Deadline (seconds)"
            min={1}
            fallback={spec.policies.deadline_seconds}
            value={spec.policies.deadline_seconds}
            disabled={readOnly}
            onChange={(v) =>
              update((s) => ({ ...s, policies: { ...s.policies, deadline_seconds: v } }))
            }
          />
          <SelectField
            label="On failure"
            value={spec.policies.on_failure}
            disabled={readOnly}
            onChange={(v) =>
              update((s) => ({ ...s, policies: { ...s.policies, on_failure: v as OnFailure } }))
            }
            options={[
              { value: 'fail_fast', label: 'Fail fast (cancel siblings)' },
              { value: 'continue', label: 'Continue independent branches' },
            ]}
          />
        </div>
      </Section>

      {advisory.length > 0 && (
        <section aria-label="Advisory checks" className="card space-y-1 text-sm">
          <h3 className="text-sm font-semibold">Checks</h3>
          <ul className="list-disc pl-5">
            {advisory.map((i, n) => (
              <li
                key={`${i.code}-${i.nodeId ?? ''}-${n}`}
                className={i.severity === 'warning' ? 'text-amber-900' : 'text-red-800'}
              >
                {i.message} <span className="font-mono text-xs">({i.code})</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

/** Keyboard-accessible alternative to the canvas; also carries the live status text. */
function NodeList({
  spec,
  selected,
  statuses,
  invalid,
  onSelect,
}: {
  spec: PipelineSpec;
  selected: string | null;
  statuses: ReadonlyMap<string, NodeRunStatus> | undefined;
  invalid: ReadonlySet<string>;
  onSelect: (id: string) => void;
}) {
  if (spec.nodes.length === 0) return null;
  return (
    <ul aria-label="Nodes" className="divide-y divide-slate-100 rounded-md border border-slate-200">
      {spec.nodes.map((n) => {
        const out = spec.edges.filter((e) => e.from === n.id);
        const status = statuses?.get(n.id);
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
            {status && <span className="font-medium">Status: {nodeStatusLabel(status)}</span>}
            {invalid.has(n.id) && <span className="text-red-700">Has problems</span>}
            {out.length > 0 && (
              <span className="text-xs text-slate-600">
                to{' '}
                {out
                  .map((e) => `${e.to}${edgeWhen(e) !== 'always' ? ` (${whenValue(e)})` : ''}`)
                  .join(', ')}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function EdgeList({
  spec,
  readOnly,
  onAdd,
  onChange,
  onRemove,
}: {
  spec: PipelineSpec;
  readOnly: boolean;
  onAdd: (from: string, to: string) => void;
  onChange: (index: number, edge: PipelineEdge) => void;
  onRemove: (index: number) => void;
}) {
  const ids = spec.nodes.map((n) => ({ value: n.id, label: n.id }));
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const typeOf = (id: string) => spec.nodes.find((n) => n.id === id)?.type;
  return (
    <Section
      title="Edges"
      help="An edge runs its target after its source. Edges leaving a condition node branch on true or false; cycles are rejected."
    >
      {spec.edges.length === 0 && <p className="text-sm text-slate-600">No edges.</p>}
      <ul className="space-y-2">
        {spec.edges.map((e, i) => (
          <li key={edgeKey(e)} className="flex flex-wrap items-end gap-2">
            <span className="pb-2 text-sm">
              {e.from} {'->'} {e.to}
            </span>
            {typeOf(e.from) === 'condition' &&
              (whenValue(e) === 'custom' ? (
                <span className="pb-2 text-xs text-slate-600">Custom condition (edit as JSON)</span>
              ) : (
                <SelectField
                  label={`When for edge ${e.from} to ${e.to}`}
                  value={whenValue(e)}
                  disabled={readOnly}
                  onChange={(w) => onChange(i, withEdgeWhen(e, w as EdgeWhenValue))}
                  options={[
                    { value: 'always', label: 'always' },
                    { value: 'true', label: 'true' },
                    { value: 'false', label: 'false' },
                  ]}
                />
              ))}
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
