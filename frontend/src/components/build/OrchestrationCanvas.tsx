'use client';

import '@xyflow/react/dist/style.css';
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from '@xyflow/react';
import { useEffect } from 'react';
import type { OrchestrationSpec } from '@/lib/api/operate-types';
import { autoLayout } from '@/lib/graph';

const TYPE_CLASS: Record<string, string> = {
  agent: '!border-blue-600 !bg-blue-50',
  tool: '!border-green-600 !bg-green-50',
  condition: '!border-amber-600 !bg-amber-50',
  approval: '!border-purple-600 !bg-purple-50',
  parallel: '!border-slate-600 !bg-slate-100',
};

export function toFlowEdges(spec: OrchestrationSpec): Edge[] {
  return spec.edges.map((e, i) => ({
    id: `e${i}-${e.from}-${e.to}-${e.when ?? 'always'}`,
    source: e.from,
    target: e.to,
    label: e.when && e.when !== 'always' ? e.when : undefined,
    markerEnd: { type: MarkerType.ArrowClosed },
  }));
}

/**
 * React Flow canvas over the spec. The spec is the source of truth: structure changes flow out through
 * callbacks; drag positions are session-only (the orchestration contract has no layout field).
 */
export function OrchestrationCanvas({
  spec,
  selectedId,
  invalidIds,
  readOnly,
  onSelect,
  onConnect,
  onDeleteNodes,
  onDeleteEdges,
}: {
  spec: OrchestrationSpec;
  selectedId: string | null;
  invalidIds: ReadonlySet<string>;
  readOnly: boolean;
  onSelect: (id: string | null) => void;
  onConnect: (from: string, to: string) => void;
  onDeleteNodes: (ids: string[]) => void;
  onDeleteEdges: (edges: { from: string; to: string }[]) => void;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    const layout = autoLayout(spec.nodes, spec.edges);
    setNodes((prev) =>
      spec.nodes.map((n) => {
        const existing = prev.find((p) => p.id === n.id);
        return {
          id: n.id,
          position: existing?.position ?? layout.get(n.id) ?? { x: 0, y: 0 },
          data: { label: `${n.id} (${n.type})` },
          selected: n.id === selectedId,
          ariaLabel: `${n.type} node ${n.id}${invalidIds.has(n.id) ? ', has problems' : ''}`,
          className: `${TYPE_CLASS[n.type] ?? ''} ${invalidIds.has(n.id) ? '!border-red-600 !border-2' : ''}`,
        };
      }),
    );
    setEdges(toFlowEdges(spec));
  }, [spec, selectedId, invalidIds, setNodes, setEdges]);

  return (
    <div
      aria-label="Orchestration graph canvas"
      role="application"
      className="h-[28rem] rounded-md border border-slate-300 bg-white"
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodesConnectable={!readOnly}
        nodesDraggable={!readOnly}
        deleteKeyCode={readOnly ? null : ['Backspace', 'Delete']}
        onConnect={(c: Connection) => {
          if (c.source && c.target) onConnect(c.source, c.target);
        }}
        onNodeClick={(_, n) => onSelect(n.id)}
        onPaneClick={() => onSelect(null)}
        onNodesDelete={(ns) => onDeleteNodes(ns.map((n) => n.id))}
        onEdgesDelete={(es) => onDeleteEdges(es.map((e) => ({ from: e.source, to: e.target })))}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
