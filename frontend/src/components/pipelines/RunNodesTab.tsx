'use client';

import type { Run } from '@/lib/api/operate-types';
import { useRunOverlay } from '@/lib/pipeline-hooks';
import { QueryError } from '../QueryError';
import { NodeStatusList } from './NodeStatusList';

/** "Nodes" tab of the run detail for kind "pipeline": per-node status, outputs and artifacts. */
export function RunNodesTab({ org, run }: { org: string; run: Run }) {
  const { nodes, runError } = useRunOverlay(org, run.id);
  if (runError) return <QueryError error={runError} />;
  if (!nodes) return <p role="status">Loading nodes...</p>;
  return (
    <section aria-label="Pipeline nodes" className="card p-0">
      <NodeStatusList org={org} run={run} nodes={nodes} />
    </section>
  );
}
