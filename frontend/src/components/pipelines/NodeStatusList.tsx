'use client';

import Link from 'next/link';
import { useState } from 'react';
import type { Run } from '@/lib/api/operate-types';
import type { NodeRun, NodeRunStatus } from '@/lib/api/pipeline-types';
import { useRunArtifact } from '@/lib/pipeline-hooks';
import { nodeDisplayStatus, nodeStatusLabel } from '@/lib/pipeline-spec';
import { orgHref } from '@/lib/scope';
import { Badge, formatDate, Modal, QueryGate } from '../admin/ui';
import { JsonView } from '../build/shared';

const TONE: Record<NodeRunStatus, 'neutral' | 'good' | 'warn' | 'bad'> = {
  pending: 'neutral',
  running: 'warn',
  succeeded: 'good',
  failed: 'bad',
  skipped: 'neutral',
  cancelled: 'neutral',
  waiting_for_approval: 'warn',
  outcome_unknown: 'warn',
};

/** The server sends `null` (not absence) for "no preview". */
const hasPreview = (n: NodeRun): boolean =>
  n.output_preview !== undefined && n.output_preview !== null;

/** Artifact id from `output_ref`: a bare string or `{artifact_id|id}`. */
export function artifactIdOf(ref: unknown): string | undefined {
  if (typeof ref === 'string' && ref) return ref;
  if (typeof ref === 'object' && ref !== null) {
    const r = ref as Record<string, unknown>;
    const id = r.artifact_id ?? r.id;
    if (typeof id === 'string' && id) return id;
  }
  return undefined;
}

export function NodeStatusChip({ status }: { status: NodeRunStatus }) {
  return (
    <span aria-label={`Node status: ${nodeStatusLabel(status)}`}>
      <Badge tone={TONE[status] ?? 'neutral'}>{nodeStatusLabel(status)}</Badge>
    </span>
  );
}

/** Per-node status table (the accessible counterpart of the canvas overlay) with output preview drawer. */
export function NodeStatusList({
  org,
  run,
  nodes,
}: {
  org: string;
  run: Run;
  nodes: readonly NodeRun[];
}) {
  const [open, setOpen] = useState<NodeRun | null>(null);
  if (nodes.length === 0) return <p className="text-sm text-slate-600">No node activity yet.</p>;
  return (
    <>
      <table className="w-full">
        <caption className="sr-only">Node statuses</caption>
        <thead className="border-b border-slate-200">
          <tr>
            {['Node', 'Type', 'Status', 'Attempts', 'Started', 'Finished', 'Output'].map((h) => (
              <th key={h} scope="col" className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {nodes.map((n) => (
            <tr key={n.node_id}>
              <td className="td font-mono text-xs">{n.node_id}</td>
              <td className="td">{n.type}</td>
              <td className="td">
                <NodeStatusChip status={nodeDisplayStatus(n)} />
                {n.error_class && <div className="text-xs text-red-800">{n.error_class}</div>}
                {(n.outcome_unknown || n.status === 'waiting_for_approval') && (
                  <div>
                    <Link
                      className="text-xs text-blue-800 underline"
                      href={
                        n.outcome_unknown
                          ? `${orgHref(org)}/runs/${run.id}?tab=overview#decision`
                          : `${orgHref(org)}/approvals`
                      }
                    >
                      {n.outcome_unknown
                        ? `Open the decision for ${n.node_id}`
                        : `Open the approval for ${n.node_id}`}
                    </Link>
                  </div>
                )}
              </td>
              <td className="td">{n.attempts}</td>
              <td className="td">{formatDate(n.started_at)}</td>
              <td className="td">{formatDate(n.finished_at)}</td>
              <td className="td">
                {hasPreview(n) || artifactIdOf(n.output_ref) ? (
                  <button
                    type="button"
                    className="btn"
                    aria-label={`View output of ${n.node_id}`}
                    onClick={() => setOpen(n)}
                  >
                    View output
                  </button>
                ) : (
                  '-'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {open && <OutputDrawer org={org} run={run} node={open} onClose={() => setOpen(null)} />}
    </>
  );
}

function OutputDrawer({
  org,
  run,
  node,
  onClose,
}: {
  org: string;
  run: Run;
  node: NodeRun;
  onClose: () => void;
}) {
  const artifactId = artifactIdOf(node.output_ref);
  return (
    <Modal title={`Output of ${node.node_id}`} onClose={onClose} wide>
      <div className="space-y-3">
        {hasPreview(node) && (
          <div>
            <h4 className="text-sm font-semibold">Preview</h4>
            <p className="text-xs text-slate-600">Redacted by the server and capped at 4 KiB.</p>
            <JsonView label={`Output preview of ${node.node_id}`} value={node.output_preview} />
          </div>
        )}
        {artifactId && <ArtifactViewer org={org} runId={run.id} artifactId={artifactId} />}
        {!hasPreview(node) && !artifactId && (
          <p className="text-sm text-slate-600">No output was recorded for this node.</p>
        )}
        <div className="flex justify-end">
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** Stored node output (size-capped and redacted by the server); shown as text, never as HTML. */
export function ArtifactViewer({
  org,
  runId,
  artifactId,
}: {
  org: string;
  runId: string;
  artifactId: string;
}) {
  const query = useRunArtifact(org, runId, artifactId);
  return (
    <section aria-label="Stored artifact">
      <h4 className="text-sm font-semibold">Stored artifact</h4>
      <QueryGate query={query} noun="artifact">
        {(data) => (
          <div className="space-y-1">
            {data.truncated && (
              <p role="note" className="text-xs text-amber-900">
                This artifact was truncated by the server ({data.size_bytes} bytes stored).
              </p>
            )}
            {data.redacted && (
              <p role="note" className="text-xs text-slate-600">
                Secret values were masked.
              </p>
            )}
            {typeof data.content === 'string' ? (
              <pre
                tabIndex={0}
                aria-label="Artifact content"
                className="max-h-96 overflow-auto rounded bg-slate-100 p-2 font-mono text-xs"
              >
                {data.content}
              </pre>
            ) : (
              <JsonView label="Artifact content" value={data.content ?? null} />
            )}
          </div>
        )}
      </QueryGate>
    </section>
  );
}
