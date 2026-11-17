import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { artifact } from '@/test/build';
import { run } from '@/test/operate';
import { nodeRun, stubPipelines } from '@/test/pipelines';
import { list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { emptyPipelineSpec } from '@/lib/pipeline-spec';
import { ArtifactDetailView } from '../build/ArtifactDetailView';
import { ArtifactListView } from '../build/ArtifactListView';
import { RunDetailView } from '../operate/RunDetailView';
import type { PipelineCanvasProps } from './PipelineCanvas';
import { PipelineHistory } from './PipelineHistory';
import { SchedulesPanel } from './SchedulesPanel';

vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock('./LazyPipelineCanvas', () => ({
  LazyPipelineCanvas: (p: PipelineCanvasProps) => (
    <div data-testid="canvas" data-invalid-nodes={[...p.invalidNodeIds].sort().join(',')} />
  ),
}));
afterEach(() => vi.unstubAllGlobals());

const P = '/api/v1/orgs/acme/pipelines';

describe('Pipeline Registry', () => {
  it('lists pipelines through the versioned-artifact list', async () => {
    stubPipelines({
      [`GET ${P}`]: () =>
        list([artifact({ id: 'p-1', key: 'customer-risk-review', name: 'Risk review' })]),
    });
    renderWithClient(<ArtifactListView kind="pipelines" />);
    expect(await screen.findByRole('link', { name: 'Risk review' })).toHaveAttribute(
      'href',
      '/o/acme/build/pipelines/p-1',
    );
    expect(screen.getByRole('heading', { name: 'Pipelines' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create pipeline' })).toBeInTheDocument();
  });

  it('hides Create without pipeline:create', async () => {
    stubPipelines({ [`GET ${P}`]: () => list([]) }, ['pipeline:read']);
    renderWithClient(<ArtifactListView kind="pipelines" />);
    await screen.findByText('No pipelines match.');
    expect(screen.queryByRole('button', { name: 'Create pipeline' })).not.toBeInTheDocument();
  });
});

describe('Pipeline detail (validation)', () => {
  const spec = {
    ...emptyPipelineSpec(),
    nodes: [
      { id: 'a', type: 'db_query', config: {}, inputs: {} },
      { id: 'b', type: 'transform', config: {}, inputs: {} },
    ],
    edges: [{ from: 'a', to: 'b' }],
  };
  const routes = () => ({
    [`GET ${P}/p-1`]: () => jsonResponse(artifact({ id: 'p-1', name: 'Risk review' })),
    [`GET ${P}/p-1/draft`]: () =>
      jsonResponse({ id: 'd-1', version_no: 2, state: 'draft', row_version: 3, spec }),
  });

  it('shows errors grouped by code, highlights the erroring node and shows the topo order', async () => {
    const fetchMock = stubPipelines({
      ...routes(),
      [`POST ${P}/p-1/draft/validate`]: () =>
        jsonResponse({
          valid: false,
          errors: [
            {
              path: 'nodes[1]',
              code: 'schema_incompatible',
              node_id: 'b',
              message: 'rows mismatch',
            },
            { path: 'nodes[1]', code: 'schema_incompatible', node_id: 'b', message: 'second' },
            { path: 'nodes[0]', code: 'ref_not_published', node_id: 'a', message: 'agent draft' },
          ],
          warnings: [
            {
              path: 'nodes[1]',
              code: 'approval_required_before_side_effect',
              message: 'no approval',
            },
          ],
          topo_order: ['a', 'b'],
        }),
    });
    renderWithClient(<ArtifactDetailView kind="pipelines" id="p-1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Validate' }));
    const panel = await screen.findByRole('region', { name: 'Validation results' });
    expect(within(panel).getByText('Pipeline has 3 errors')).toBeInTheDocument();
    expect(
      within(panel)
        .getAllByRole('heading', { level: 4 })
        .map((h) => h.textContent),
    ).toEqual(['schema_incompatible', 'ref_not_published', 'Warnings', 'Execution order']);
    expect(within(panel).getByText('rows mismatch')).toBeInTheDocument();
    expect(
      within(within(panel).getByRole('list', { name: 'Topological order' }))
        .getAllByRole('listitem')
        .map((l) => l.textContent),
    ).toEqual(['1. a', '2. b']);
    expect(screen.getByTestId('canvas')).toHaveAttribute('data-invalid-nodes', 'a,b');
    expect(fetchMock.calls.some((c) => c.key === `POST ${P}/p-1/draft/validate`)).toBe(true);
  });

  it('exposes Run, History and Schedules tabs for pipelines only', async () => {
    stubPipelines({ ...routes() });
    renderWithClient(<ArtifactDetailView kind="pipelines" id="p-1" />);
    const tabs = await screen.findByRole('tablist');
    expect(
      within(tabs)
        .getAllByRole('tab')
        .map((t) => t.textContent),
    ).toEqual(['Editor', 'Versions', 'Compare', 'Run', 'History', 'Schedules']);
  });
});

describe('Pipeline History', () => {
  it('lists runs of the pipeline, labels tests and links to run detail', async () => {
    const fetchMock = stubPipelines({
      [`${'GET'} ${P}/p-1/runs`]: () =>
        jsonResponse({
          items: [
            run({ id: 'run-a', kind: 'pipeline', status: 'completed' }),
            run({ id: 'run-b', kind: 'pipeline', status: 'failed', is_test: true }),
          ],
          next_cursor: null,
        }),
    });
    renderWithClient(<PipelineHistory org="acme" pipelineId="p-1" />);
    expect(await screen.findByRole('link', { name: 'run-a' })).toHaveAttribute(
      'href',
      '/o/acme/runs/run-a',
    );
    const rows = screen.getAllByRole('row');
    expect(within(rows[1] as HTMLElement).getByText('Execution')).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText('Test run')).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByLabelText('Run status: Failed')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `GET ${P}/p-1/runs`)).toBe(true);
  });

  it('shows an empty state', async () => {
    stubPipelines({ [`GET ${P}/p-1/runs`]: () => list([]) });
    renderWithClient(<PipelineHistory org="acme" pipelineId="p-1" />);
    expect(await screen.findByText('No runs yet.')).toBeInTheDocument();
  });
});

describe('Schedules', () => {
  it('explains that scheduling is not enabled on 501 scheduling_not_enabled and offers no controls', async () => {
    stubPipelines({
      [`GET ${P}/p-1/schedules`]: () => problemResponse(501, 'scheduling_not_enabled'),
    });
    renderWithClient(<SchedulesPanel org="acme" pipelineId="p-1" />);
    expect(
      await screen.findByText(/Scheduling is not enabled in this installation/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('shows real schedules read-only when the API returns them', async () => {
    stubPipelines({
      [`GET ${P}/p-1/schedules`]: () =>
        list([{ id: 's-1', cron: '0 6 * * *', next_run_at: '2026-11-04T06:00:00Z' }]),
    });
    renderWithClient(<SchedulesPanel org="acme" pipelineId="p-1" />);
    expect(await screen.findByText(/0 6 \* \* \*/)).toBeInTheDocument();
    expect(screen.queryByText(/not enabled/)).not.toBeInTheDocument();
  });
});

describe('SchedulesPanel server shape', () => {
  it('accepts the bare list and shows a paused schedule (status, not enabled)', async () => {
    stubPipelines({
      [`GET ${P}/p-1/schedules`]: () =>
        jsonResponse([{ id: 's-1', cron: '0 6 * * *', status: 'paused', next_run_at: null }]),
    });
    renderWithClient(<SchedulesPanel org="acme" pipelineId="p-1" />);
    expect(await screen.findByText(/0 6 \* \* \*.*\(paused\)/)).toBeInTheDocument();
  });
});

describe('Run detail Nodes tab', () => {
  const R = '/api/v1/orgs/acme/runs/run-1';
  const common = {
    [`GET ${R}/steps`]: () => list([]),
    [`GET ${R}/events`]: () => list([]),
    [`GET ${R}/result`]: () => jsonResponse({ output: 'ok' }),
  };

  it('appears for pipeline runs and lists node statuses', async () => {
    stubPipelines({
      ...common,
      [`GET ${R}`]: () => jsonResponse(run({ kind: 'pipeline', status: 'completed' })),
      [`GET ${R}/nodes`]: () =>
        jsonResponse({ items: [nodeRun({ node_id: 'src', status: 'succeeded' })] }),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: 'Nodes' }));
    expect(await screen.findByLabelText('Node status: Succeeded')).toBeInTheDocument();
  });

  it('is absent for agent runs', async () => {
    stubPipelines({
      ...common,
      [`GET ${R}`]: () => jsonResponse(run({ kind: 'agent', status: 'completed' })),
    });
    renderWithClient(<RunDetailView id="run-1" />);
    await screen.findByRole('tab', { name: 'Overview' });
    await waitFor(() =>
      expect(screen.queryByRole('tab', { name: 'Nodes' })).not.toBeInTheDocument(),
    );
  });
});
