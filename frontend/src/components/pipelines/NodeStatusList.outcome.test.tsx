import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { run } from '@/test/operate';
import { nodeRun } from '@/test/pipelines';
import { stubApi } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { nodeDisplayStatus } from '@/lib/pipeline-spec';
import { NodeStatusList } from './NodeStatusList';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

describe('NodeStatusList: unknown write outcome (R1)', () => {
  it('marks a node by its own outcome_unknown flag, whatever its class or status', () => {
    stubApi({});
    renderWithClient(
      <NodeStatusList
        org="acme"
        run={run({ kind: 'pipeline', status: 'timed_out', outcome_unknown: true })}
        nodes={[
          nodeRun({
            node_id: 'sink',
            status: 'failed',
            error_class: 'transient_network',
            outcome_unknown: true,
          }),
          nodeRun({ node_id: 'tool1', status: 'cancelled', outcome_unknown: true }),
          nodeRun({ node_id: 'live', status: 'waiting_for_approval', outcome_unknown: true }),
          nodeRun({ node_id: 'sibling', status: 'failed', error_class: 'tool_error' }),
        ]}
      />,
    );
    expect(screen.getAllByLabelText('Node status: Write outcome unknown')).toHaveLength(3);
    expect(screen.getAllByLabelText('Node status: Failed')).toHaveLength(1);
    expect(screen.queryByLabelText('Node status: Cancelled')).not.toBeInTheDocument();
    const links = screen.getAllByRole('link', { name: /Open the decision for/ });
    expect(links).toHaveLength(3);
    // Points at the run detail decision panel (Overview tab, #decision), not the approvals queue.
    for (const l of links)
      expect(l).toHaveAttribute('href', '/o/acme/runs/run-1?tab=overview#decision');
  });

  it('does not infer unknown from the run: a failed node with the old class stays Failed', () => {
    stubApi({});
    renderWithClient(
      <NodeStatusList
        org="acme"
        run={run({ kind: 'pipeline', status: 'cancelled', outcome_unknown: true })}
        nodes={[
          nodeRun({ node_id: 'n', status: 'failed', error_class: 'indeterminate_side_effect' }),
        ]}
      />,
    );
    expect(screen.getByLabelText('Node status: Failed')).toBeInTheDocument();
  });

  it('derives the canvas status from the node flag alone', () => {
    expect(nodeDisplayStatus({ status: 'failed', outcome_unknown: true })).toBe('outcome_unknown');
    expect(nodeDisplayStatus({ status: 'failed', outcome_unknown: false })).toBe('failed');
    expect(nodeDisplayStatus({ status: 'failed' })).toBe('failed');
  });
});
