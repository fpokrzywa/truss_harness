import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { emptyPipelineSpec } from '@/lib/pipeline-spec';
import { artifact } from '@/test/build';
import { run } from '@/test/operate';
import { nodeRun, stubPipelines } from '@/test/pipelines';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { PipelineEditor } from './PipelineEditor';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
vi.mock('./LazyPipelineCanvas', () => ({ LazyPipelineCanvas: () => <div /> }));
vi.mock('./PipelineRunContext', () => ({
  usePipelineRun: () => ({ runId: 'run-1', setRunId: () => undefined }),
}));
afterEach(() => vi.unstubAllGlobals());

const R = '/api/v1/orgs/acme/runs/run-1';

describe('PipelineEditor live status line (R1)', () => {
  it.each(['cancelled', 'timed_out'] as const)(
    'does not print the plain status "%s" for a run with an unknown write outcome',
    async (status) => {
      stubPipelines({
        [`GET ${R}`]: () =>
          jsonResponse(run({ id: 'run-1', kind: 'pipeline', status, outcome_unknown: true })),
        [`GET ${R}/nodes`]: () => jsonResponse({ items: [nodeRun()] }),
      });
      renderWithClient(
        <PipelineEditor
          org="acme"
          artifact={artifact({ id: 'p-1' })}
          spec={emptyPipelineSpec()}
          onChange={() => undefined}
          readOnly={false}
          reportInvalid={() => undefined}
        />,
      );
      const line = await screen.findByText(/Showing live node status for run run-1/);
      expect(await screen.findByLabelText('Run status: Write outcome unknown')).toBeInTheDocument();
      expect(line).not.toHaveTextContent(/cancelled|timed_out/);
    },
  );
});
