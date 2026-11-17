'use client';

import '@xyflow/react/dist/style.css';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position as HandlePosition,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import { useEffect } from 'react';
import type { ObjectType, OntologySpec, Position } from '@/lib/api/ontology-types';
import { layoutPositions, relationshipEdgeLabel } from '@/lib/ontology-spec';
import { useSectionFirstShown } from '../build/SectionTabs';

export type Selection = { kind: 'object_type' | 'relationship'; key: string } | null;

interface ObjectNodeData extends Record<string, unknown> {
  type: ObjectType;
  invalid: boolean;
}

/** Node card: namespace, label, key and the property list (type, classification, primary key marker). */
function ObjectNodeView({ data }: NodeProps<Node<ObjectNodeData>>) {
  const t = data.type;
  return (
    <div className="min-w-56 px-2 py-1 text-xs">
      <Handle type="target" position={HandlePosition.Left} />
      <div className="text-slate-600">{t.namespace}</div>
      <div className="font-semibold text-slate-900">{t.label}</div>
      <div className="font-mono text-slate-700">{t.key}</div>
      {data.invalid && <div className="font-medium text-red-700">Has problems</div>}
      {t.properties.length > 0 && (
        <ul className="mt-1 space-y-0.5 border-t border-slate-200 pt-1">
          {t.properties.map((p) => (
            <li key={p.key} className="flex items-center gap-1">
              {t.primary_key.includes(p.key) && (
                <abbr title="Primary key" className="font-semibold no-underline">
                  PK
                </abbr>
              )}
              <span className="font-mono">{p.key}</span>
              <span className="text-slate-600">{p.type}</span>
              <span
                className={`rounded border px-1 ${
                  p.classification === 'restricted'
                    ? 'border-red-300 bg-red-50 text-red-800'
                    : 'border-slate-300 bg-slate-100 text-slate-700'
                }`}
              >
                {p.classification}
              </span>
            </li>
          ))}
        </ul>
      )}
      <Handle type="source" position={HandlePosition.Right} />
    </div>
  );
}

const nodeTypes = { objectType: ObjectNodeView };

export interface OntologyCanvasProps {
  spec: OntologySpec;
  selected: Selection;
  invalidObjects: ReadonlySet<string>;
  invalidRelationships: ReadonlySet<string>;
  readOnly: boolean;
  onSelect: (s: Selection) => void;
  onConnect: (from: string, to: string) => void;
  onDeleteObjects: (keys: string[]) => void;
  onDeleteRelationships: (keys: string[]) => void;
  onMoveNode: (key: string, pos: Position) => void;
}

/**
 * React Flow canvas over the ontology spec (same library and conventions as the pipeline canvas). The
 * spec is the source of truth; positions persist to `spec.ui.positions` on drag stop.
 */
export function OntologyCanvas(props: OntologyCanvasProps) {
  const { spec, selected, invalidObjects, invalidRelationships, readOnly } = props;
  // This graph is not the first editor tab; drawn while hidden, it would fit itself to zero size.
  const shown = useSectionFirstShown();
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<ObjectNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    const layout = layoutPositions(spec);
    setNodes((prev) =>
      spec.object_types.map((o) => {
        const invalid = invalidObjects.has(o.key);
        return {
          id: o.key,
          type: 'objectType',
          position: prev.find((p) => p.id === o.key)?.position ??
            layout.get(o.key) ?? { x: 0, y: 0 },
          data: { type: o, invalid },
          selected: selected?.kind === 'object_type' && selected.key === o.key,
          ariaLabel: `Object type ${o.label} (${o.key})${invalid ? ', has problems' : ''}`,
          className: `rounded-md border-2 ${invalid ? '!border-red-600' : '!border-slate-500'} !bg-slate-100`,
        };
      }),
    );
    setEdges(
      spec.relationships.map((r) => {
        const bad = invalidRelationships.has(r.key);
        return {
          id: r.key,
          source: r.from,
          target: r.to,
          label: `${relationshipEdgeLabel(r)}${bad ? ' (has problems)' : ''}`,
          markerEnd: { type: MarkerType.ArrowClosed },
          selected: selected?.kind === 'relationship' && selected.key === r.key,
          ...(bad ? { style: { stroke: 'var(--color-shi-orange)', strokeWidth: 2 } } : {}),
        };
      }),
    );
  }, [spec, selected, invalidObjects, invalidRelationships, setNodes, setEdges]);

  return (
    <div
      aria-label="Ontology graph canvas"
      role="application"
      className="h-[34rem] rounded-md border border-slate-300 bg-white"
    >
      {shown && (
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodesConnectable={!readOnly}
          nodesDraggable={!readOnly}
          deleteKeyCode={readOnly ? null : ['Backspace', 'Delete']}
          onConnect={(c: Connection) => {
            if (c.source && c.target) props.onConnect(c.source, c.target);
          }}
          onNodeClick={(_, n) => props.onSelect({ kind: 'object_type', key: n.id })}
          onEdgeClick={(_, e) => props.onSelect({ kind: 'relationship', key: e.id })}
          onPaneClick={() => props.onSelect(null)}
          onNodeDragStop={(_, n) => props.onMoveNode(n.id, n.position)}
          onNodesDelete={(ns) => props.onDeleteObjects(ns.map((n) => n.id))}
          onEdgesDelete={(es) => props.onDeleteRelationships(es.map((e) => e.id))}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      )}
    </div>
  );
}
