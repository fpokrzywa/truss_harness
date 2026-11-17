import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NodeRunStatus, PipelineSpec } from '@/lib/api/pipeline-types';
import { emptyPipelineSpec } from '@/lib/pipeline-spec';
import { artifact } from '@/test/build';
import { run } from '@/test/operate';
import { nodeRun, stubPipelines } from '@/test/pipelines';
import { list, stubApi } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { NodeStatusList } from './NodeStatusList';
import { PipelineCanvas, type PipelineCanvasProps } from './PipelineCanvas';
import { PipelineRunProvider } from './PipelineRunContext';
import { PipelineRunPanel, PipelineTestPanel } from './PipelineRunPanels';
import { NODE_TYPES } from '@/test/pipelines';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
vi.mock('./LazyPipelineCanvas', () => ({
  LazyPipelineCanvas: (p: PipelineCanvasProps) => (
    <div data-testid="canvas" data-statuses={JSON.stringify([...(p.statuses ?? [])])} />
  ),
}));
beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => vi.unstubAllGlobals());

const R = '/api/v1/orgs/acme/runs';
const WS = [{ id: 'ws-1', slug: 'eng', name: 'Engineering', status: 'active', row_version: 1 }];
const PJ = [{ id: 'pj-1', slug: 'api', name: 'API', status: 'active', row_version: 1 }];

const paramSpec = (): PipelineSpec => ({
  ...emptyPipelineSpec(),
  params: {
    type: 'object',
    required: ['region'],
    properties: {
      region: { type: 'string', title: 'Region' },
      limit: { type: 'integer', minimum: 1 },
    },
  },
  nodes: [
    { id: 'src', type: 'db_query', config: {}, inputs: {} },
    { id: 'xf', type: 'transform', config: {}, inputs: {} },
  ],
  edges: [{ from: 'src', to: 'xf' }],
});

function routes(extra: Parameters<typeof stubPipelines>[0] = {}) {
  return {
    'GET /api/v1/orgs/acme/workspaces': () => list(WS),
    'GET /api/v1/orgs/acme/workspaces/eng/projects': () => list(PJ),
    ...extra,
  };
}

async function pickTarget(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole('option', { name: 'Engineering' });
  await user.selectOptions(screen.getByLabelText('Run in workspace'), 'eng');
  await waitFor(() => expect(screen.getByRole('option', { name: 'API' })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText('Run in project'), 'api');
}

describe('PipelineTestPanel', () => {
  const renderPanel = (dirty = false) =>
    renderWithClient(
      <PipelineRunProvider>
        <PipelineTestPanel
          org="acme"
          artifact={artifact({ id: 'p-1' })}
          spec={paramSpec()}
          dirty={dirty}
        />
      </PipelineRunProvider>,
    );

  it('validates the params form before sending anything', async () => {
    const fetchMock = stubPipelines(routes());
    renderPanel();
    const user = userEvent.setup();
    await pickTarget(user);
    await user.type(screen.getByLabelText('limit'), '0');
    await user.click(screen.getByRole('button', { name: 'Run test' }));
    expect(screen.getByText('Region is required.')).toBeInTheDocument();
    expect(screen.getByText('Must be at least 1.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key === `POST ${R}`)).toBe(false);
  });

  it('starts a draft test run with version_id "draft", is_test and an Idempotency-Key', async () => {
    const fetchMock = stubPipelines(
      routes({
        [`POST ${R}`]: () =>
          jsonResponse(run({ id: 'run-9', kind: 'pipeline', is_test: true }), { status: 202 }),
        [`GET ${R}/run-9`]: () =>
          jsonResponse(run({ id: 'run-9', kind: 'pipeline', is_test: true, status: 'completed' })),
        [`GET ${R}/run-9/nodes`]: () =>
          jsonResponse({ items: [nodeRun({ node_id: 'src', status: 'succeeded' })] }),
      }),
    );
    renderPanel();
    const user = userEvent.setup();
    await pickTarget(user);
    await user.type(screen.getByLabelText('Region (required)'), 'eu');
    await user.type(screen.getByLabelText('limit'), '5');
    await user.click(screen.getByRole('button', { name: 'Run test' }));
    const post = await waitFor(() => {
      const c = fetchMock.calls.find((x) => x.key === `POST ${R}`);
      if (!c) throw new Error('not sent');
      return c;
    });
    expect(JSON.parse(post.init.body as string)).toEqual({
      kind: 'pipeline',
      pipeline_id: 'p-1',
      version_id: 'draft',
      workspace: 'eng',
      project: 'api',
      params: { region: 'eu', limit: 5 },
      is_test: true,
    });
    expect((post.init.headers as Record<string, string>)['Idempotency-Key']).toMatch(
      /^[0-9a-f-]{36}$/,
    );
    const monitor = await screen.findByRole('region', { name: 'Pipeline run' });
    expect(within(monitor).getByText('Test run')).toBeInTheDocument();
    expect(await within(monitor).findByLabelText('Node status: Succeeded')).toBeInTheDocument();
  });

  it('maps server 422 paths onto the matching parameter field', async () => {
    stubPipelines(
      routes({
        [`POST ${R}`]: () =>
          problemResponse(422, 'validation_error', {
            errors: [
              { loc: ['body', 'params', 'region'], msg: 'Unknown region', type: 'value_error' },
            ],
          }),
      }),
    );
    renderPanel();
    const user = userEvent.setup();
    await pickTarget(user);
    await user.type(screen.getByLabelText('Region (required)'), 'mars');
    await user.click(screen.getByRole('button', { name: 'Run test' }));
    await screen.findAllByText('Unknown region');
    expect(screen.getByLabelText('Region (required)')).toHaveAccessibleDescription(
      'Unknown region',
    );
  });

  it('is disabled while the draft has unsaved changes', async () => {
    stubPipelines(routes());
    renderPanel(true);
    expect(await screen.findByText(/Save the draft to test it/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run test' })).toBeDisabled();
  });

  it('is hidden without pipeline:test', async () => {
    stubPipelines(routes(), ['pipeline:read']);
    renderPanel();
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Test the draft' })).toBeNull(),
    );
  });
});

describe('PipelineRunPanel (execution)', () => {
  const art = artifact({ id: 'p-1', latest_published_version: { id: 'v-2', version_no: 2 } });
  const base = {
    'GET /api/v1/orgs/acme/pipelines/p-1/versions': () =>
      list([
        { id: 'v-2', version_no: 2, state: 'published' },
        { id: 'v-1', version_no: 1, state: 'published' },
      ]),
    'GET /api/v1/orgs/acme/pipelines/p-1/versions/v-2': () =>
      jsonResponse({ id: 'v-2', version_no: 2, state: 'published', spec: paramSpec() }),
  };

  it('runs the published version (no draft, no is_test) and overlays node statuses', async () => {
    const fetchMock = stubPipelines(
      routes({
        ...base,
        [`POST ${R}`]: () => jsonResponse(run({ id: 'run-5', kind: 'pipeline' }), { status: 202 }),
        [`GET ${R}/run-5`]: () =>
          jsonResponse(run({ id: 'run-5', kind: 'pipeline', status: 'completed' })),
        [`GET ${R}/run-5/nodes`]: () =>
          jsonResponse({
            items: [
              nodeRun({ node_id: 'src', status: 'succeeded' }),
              nodeRun({ node_id: 'xf', type: 'transform', status: 'skipped' }),
            ],
          }),
      }),
    );
    renderWithClient(
      <PipelineRunProvider>
        <PipelineRunPanel org="acme" artifact={art} />
      </PipelineRunProvider>,
    );
    const user = userEvent.setup();
    await pickTarget(user);
    await user.type(await screen.findByLabelText('Region (required)'), 'eu');
    await user.click(screen.getByRole('button', { name: 'Run pipeline' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `POST ${R}`)).toBe(true));
    const post = fetchMock.calls.find((c) => c.key === `POST ${R}`);
    const body = JSON.parse(post?.init.body as string) as Record<string, unknown>;
    expect(body).toMatchObject({ version_id: 'v-2', params: { region: 'eu' } });
    expect(body).not.toHaveProperty('is_test');
    await waitFor(() =>
      expect(screen.getByTestId('canvas')).toHaveAttribute(
        'data-statuses',
        JSON.stringify([
          ['src', 'succeeded'],
          ['xf', 'skipped'],
        ]),
      ),
    );
  });

  it('offers nothing to run before the first publish', async () => {
    stubPipelines(routes());
    renderWithClient(
      <PipelineRunProvider>
        <PipelineRunPanel
          org="acme"
          artifact={artifact({ id: 'p-1', latest_published_version: null })}
        />
      </PipelineRunProvider>,
    );
    expect(await screen.findByText(/Publish this pipeline to run it/)).toBeInTheDocument();
  });
});

describe('status overlay', () => {
  const STATUSES: NodeRunStatus[] = [
    'pending',
    'running',
    'succeeded',
    'failed',
    'skipped',
    'cancelled',
    'waiting_for_approval',
  ];
  const LABELS = [
    'Pending',
    'Running',
    'Succeeded',
    'Failed',
    'Skipped',
    'Cancelled',
    'Waiting for approval',
  ];

  it('maps every node status to accessible text on the canvas nodes', async () => {
    const spec = emptyPipelineSpec();
    spec.nodes = STATUSES.map((s) => ({ id: `n_${s}`, type: 'transform', config: {}, inputs: {} }));
    const props: PipelineCanvasProps = {
      spec,
      registry: NODE_TYPES,
      selectedId: null,
      invalidNodeIds: new Set(),
      invalidEdgeKeys: new Set(),
      statuses: new Map(STATUSES.map((s) => [`n_${s}`, s])),
      readOnly: true,
      onSelect: () => undefined,
      onConnect: () => undefined,
      onDeleteNodes: () => undefined,
      onDeleteEdges: () => undefined,
      onMoveNode: () => undefined,
    };
    render(<PipelineCanvas {...props} />);
    for (const [i, s] of STATUSES.entries()) {
      const node = await screen.findByLabelText(`transform node n_${s}, status ${LABELS[i]}`);
      expect(node).toHaveTextContent(LABELS[i] as string);
    }
  });

  it('shows typed ports from the registry on canvas nodes', async () => {
    const spec = emptyPipelineSpec();
    spec.nodes = [{ id: 'xf', type: 'transform', config: {}, inputs: {} }];
    render(
      <PipelineCanvas
        spec={spec}
        registry={NODE_TYPES}
        selectedId={null}
        invalidNodeIds={new Set(['xf'])}
        invalidEdgeKeys={new Set()}
        readOnly
        onSelect={() => undefined}
        onConnect={() => undefined}
        onDeleteNodes={() => undefined}
        onDeleteEdges={() => undefined}
        onMoveNode={() => undefined}
      />,
    );
    const node = await screen.findByLabelText('transform node xf, has problems');
    expect(node).toHaveTextContent('in: rows');
    expect(node).toHaveTextContent('out: rows');
    expect(node).toHaveTextContent('Has problems');
  });
});

describe('NodeStatusList', () => {
  it('links waiting nodes to approvals and opens output preview and the stored artifact', async () => {
    stubApi({
      [`GET ${R}/run-1/artifacts/art-7`]: () =>
        jsonResponse({
          id: 'art-7',
          run_id: 'run-1',
          kind: 'node_output',
          size_bytes: 4096,
          created_at: '2026-11-03T10:00:00Z',
          truncated: true,
          redacted: true,
          content: { rows: [{ id: 1 }] },
        }),
    });
    renderWithClient(
      <NodeStatusList
        org="acme"
        run={run({ id: 'run-1', kind: 'pipeline' })}
        nodes={[
          nodeRun({ node_id: 'gate', type: 'approval', status: 'waiting_for_approval' }),
          nodeRun({
            node_id: 'src',
            output_preview: { rows: 3 },
            output_ref: 'art-7',
          }),
          nodeRun({ node_id: 'bad', status: 'failed', error_class: 'ConnectionError' }),
        ]}
      />,
    );
    expect(screen.getByRole('link', { name: 'Open the approval for gate' })).toHaveAttribute(
      'href',
      '/o/acme/approvals',
    );
    expect(screen.getByText('ConnectionError')).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'View output of src' }));
    expect(screen.getByLabelText('Output preview of src')).toHaveTextContent('"rows": 3');
    const content = await screen.findByLabelText('Artifact content');
    // The stored document is unwrapped from the server envelope, not shown as {id, run_id, ...}.
    expect(content).toHaveTextContent('"id": 1');
    expect(content).not.toHaveTextContent('run_id');
    expect(screen.getByText(/truncated by the server/)).toBeInTheDocument();
    expect(screen.getByText(/masked/)).toBeInTheDocument();
  });

  it('offers no output button for nodes whose preview and artifact are null', () => {
    stubApi({});
    renderWithClient(
      <NodeStatusList
        org="acme"
        run={run({ id: 'run-1', kind: 'pipeline' })}
        nodes={[nodeRun({ node_id: 'idle', output_preview: null, output_ref: null })]}
      />,
    );
    expect(screen.queryByRole('button', { name: 'View output of idle' })).not.toBeInTheDocument();
  });
});
