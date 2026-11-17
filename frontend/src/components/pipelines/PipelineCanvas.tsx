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
  type ReactFlowInstance,
} from '@xyflow/react';
import { useEffect, useRef } from 'react';
import type { NodeRunStatus, NodeTypeDef, PipelineSpec, Position } from '@/lib/api/pipeline-types';
import {
  edgeKey,
  edgeLabel,
  layoutPositions,
  NODE_STATUS_CLASS,
  nodeStatusLabel,
} from '@/lib/pipeline-spec';

export const DRAG_MIME = 'application/x-shifde-node-type';

interface PipelineNodeData extends Record<string, unknown> {
  nodeId: string;
  typeLabel: string;
  inputs: string[];
  outputs: string[];
  status?: NodeRunStatus;
  invalid: boolean;
}

/** Node card: id, type, typed ports from the registry and (during a run) a text status. */
function PipelineNodeView({ data }: NodeProps<Node<PipelineNodeData>>) {
  return (
    <div className="min-w-40 px-2 py-1 text-xs">
      <Handle type="target" position={HandlePosition.Left} />
      <div className="font-semibold text-slate-900">{data.nodeId}</div>
      <div className="text-slate-700">{data.typeLabel}</div>
      {data.inputs.length > 0 && <div className="text-slate-600">in: {data.inputs.join(', ')}</div>}
      {data.outputs.length > 0 && (
        <div className="text-slate-600">out: {data.outputs.join(', ')}</div>
      )}
      {data.status && <div className="mt-1 font-medium">{nodeStatusLabel(data.status)}</div>}
      {data.invalid && <div className="font-medium text-red-700">Has problems</div>}
      <Handle type="source" position={HandlePosition.Right} />
    </div>
  );
}

const nodeTypes = { pipeline: PipelineNodeView };

export interface PipelineCanvasProps {
  spec: PipelineSpec;
  registry: readonly NodeTypeDef[];
  selectedId: string | null;
  invalidNodeIds: ReadonlySet<string>;
  invalidEdgeKeys: ReadonlySet<string>;
  /** Live run overlay: node id -> status. */
  statuses?: ReadonlyMap<string, NodeRunStatus>;
  readOnly: boolean;
  onSelect: (id: string | null) => void;
  onConnect: (from: string, to: string) => void;
  onDeleteNodes: (ids: string[]) => void;
  onDeleteEdges: (edges: { from: string; to: string }[]) => void;
  onMoveNode: (id: string, pos: Position) => void;
  /** A palette item was dropped on the canvas. */
  onDropType?: (type: string, pos: Position) => void;
}

/**
 * React Flow canvas over the pipeline spec (same library and structural conventions as the orchestration
 * canvas). The spec is the source of truth; positions persist to `spec.ui.positions` on drag stop.
 */
export function PipelineCanvas(props: PipelineCanvasProps) {
  const { spec, registry, selectedId, invalidNodeIds, invalidEdgeKeys, statuses, readOnly } = props;
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<PipelineNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const instance = useRef<ReactFlowInstance<Node<PipelineNodeData>, Edge> | null>(null);

  useEffect(() => {
    const layout = layoutPositions(spec);
    setNodes((prev) =>
      spec.nodes.map((n) => {
        const def = registry.find((t) => t.type === n.type);
        const status = statuses?.get(n.id);
        const invalid = invalidNodeIds.has(n.id);
        return {
          id: n.id,
          type: 'pipeline',
          position: prev.find((p) => p.id === n.id)?.position ?? layout.get(n.id) ?? { x: 0, y: 0 },
          data: {
            nodeId: n.id,
            typeLabel: def?.label ?? n.type,
            inputs: (def?.inputs ?? []).map((p) => p.name),
            outputs: (def?.outputs ?? []).map((p) => p.name),
            status,
            invalid,
          },
          selected: n.id === selectedId,
          ariaLabel: `${n.type} node ${n.id}${status ? `, status ${nodeStatusLabel(status)}` : ''}${invalid ? ', has problems' : ''}`,
          className: `rounded-md border-2 ${status ? NODE_STATUS_CLASS[status] : '!border-slate-500 !bg-slate-100'} ${invalid ? '!border-red-600' : ''}`,
        };
      }),
    );
    setEdges(
      spec.edges.map((e, i) => {
        const bad = invalidEdgeKeys.has(edgeKey(e));
        return {
          id: `e${i}-${edgeKey(e)}`,
          source: e.from,
          target: e.to,
          label: edgeLabel(e),
          markerEnd: { type: MarkerType.ArrowClosed },
          ...(bad
            ? { style: { stroke: 'var(--color-shi-orange)', strokeWidth: 2 }, animated: false }
            : {}),
        };
      }),
    );
  }, [spec, registry, selectedId, invalidNodeIds, invalidEdgeKeys, statuses, setNodes, setEdges]);

  return (
    <div
      aria-label="Pipeline graph canvas"
      role="application"
      className="h-[30rem] rounded-md border border-slate-300 bg-white"
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onInit={(i) => {
          instance.current = i;
        }}
        nodesConnectable={!readOnly}
        nodesDraggable={!readOnly}
        deleteKeyCode={readOnly ? null : ['Backspace', 'Delete']}
        onConnect={(c: Connection) => {
          if (c.source && c.target) props.onConnect(c.source, c.target);
        }}
        onNodeClick={(_, n) => props.onSelect(n.id)}
        onPaneClick={() => props.onSelect(null)}
        onNodeDragStop={(_, n) => props.onMoveNode(n.id, n.position)}
        onNodesDelete={(ns) => props.onDeleteNodes(ns.map((n) => n.id))}
        onEdgesDelete={(es) =>
          props.onDeleteEdges(es.map((e) => ({ from: e.source, to: e.target })))
        }
        onDragOver={(e) => {
          if (readOnly || !props.onDropType) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = 'copy';
        }}
        onDrop={(e) => {
          if (readOnly || !props.onDropType) return;
          const type = e.dataTransfer.getData(DRAG_MIME);
          if (!type) return;
          e.preventDefault();
          const pos = instance.current?.screenToFlowPosition({ x: e.clientX, y: e.clientY });
          props.onDropType(type, pos ?? { x: 0, y: 0 });
        }}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
